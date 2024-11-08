import time
import cv2
import threading
import numpy as np
from adb_utils import (
    connect_to_emulator,
    click_position,
    take_screenshot,
    drag_position,
)
from loaders import load_template_images, load_all_cards
import uuid
import os
from deck import deck_info, save_deck
from card_data_manager import CardDataManager
from image_utils import ImageProcessor
from battle_actions import BattleActions
from constants import default_pokemon_stats, bench_positions, card_offset_mapping
import subprocess
import requests


class PokemonBot:
    def __init__(self, app_state, log_callback, ui_instance):
        self.app_state = app_state
        self.log_callback = log_callback
        self.running = False
        self.template_images = load_template_images("images")
        self.card_images = load_all_cards("images/cards")
        self.deck_info = deck_info
        self.card_data_manager = CardDataManager()
        self.ui_instance = ui_instance

        ## COORDS
        self.zoom_card_region = (80, 255, 740, 1020)
        self.turn_check_region = (50, 1560, 200, 20)
        self.center_x = 400
        self.center_y = 900
        self.card_start_x = 500
        self.card_y = 1500
        self.number_of_cards_region = (790, 1325, 60, 50)

        ## STATE
        self.hand_state = []
        self.active_pokemon = []
        self.bench_pokemon = []
        self.number_of_cards = None

        self.image_processor = ImageProcessor(self.log_callback)
        self.battle_actions = BattleActions(
            self.image_processor,
            self.template_images,
            self.card_images,
            self.zoom_card_region,
            self.number_of_cards_region,
            self.log_callback,
        )

    def start(self):
        if not self.app_state.program_path:
            self.log_callback("Please select emulator path first.")
            return
        self.running = True
        threading.Thread(target=self.connect_and_run).start()

    def stop(self):
        self.running = False

    def get_emulator_name(self):
        try:
            result = subprocess.run(
                ["adb", "devices"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            output = result.stdout

            lines = output.splitlines()

            devices = [line.split("\t")[0] for line in lines[1:] if "device" in line]

            if devices:
                return devices[0]
            else:
                return None
        except Exception as e:
            print(f"Error getting emulator name: {e}")
            return None

    def connect_and_run(self):
        if self.app_state.emulator_name:
            connect_to_emulator(self.app_state.emulator_name)
        else:
            emulator_name = self.get_emulator_name()
            connect_to_emulator(emulator_name)
        self.log_callback("Connected to emulator")
        self.run_script()

    def run_script(self):
        while self.running:
            # Reset active and bench Pokémon
            self.active_pokemon = []
            self.bench_pokemon = []

            # Capture screenshot
            screenshot = take_screenshot()

            ### GO THROUGH MENUS TO FIND A BATTLE
            if not self.image_processor.check_and_click(
                screenshot, self.template_images["BATTLE_ALREADY_SCREEN"], "Battle already screen"
            ):
                self.image_processor.check_and_click(
                    screenshot, self.template_images["BATTLE_SCREEN"], "Battle screen"
                )
            time.sleep(4)

            # Perform actions to search for a battle
            self.battle_actions.perform_search_battle_actions(self.running, self.stop, run_event=True)

            ### BATTLE START
            self.image_processor.check_and_click_until_found(
                self.template_images["TIME_LIMIT_INDICATOR"], 'Time limit indicator', self.running, self.stop
            )

            # Capture updated screenshot
            screenshot = take_screenshot()

            while not self.game_ended(screenshot) and not self.next_step_available(screenshot):
                # Check if Pokémon has been defeated or if Sabrina card needs to be used
                self.click_bench_pokemons()

                self.check_active_pokemon()
                self.reset_view()

                screenshot = take_screenshot()
                if self.image_processor.check(
                    screenshot, self.template_images["GOING_FIRST_INDICATOR"], None
                ) or self.image_processor.check(
                    screenshot, self.template_images["GOING_SECOND_INDICATOR"], None
                ):
                    # Log the game start based on which player goes first
                    self.log_callback("🎮 **First turn!")
                    self.check_n_cards()
                    self.reset_view()

                    if self.number_of_cards:
                        self.log_callback(f"Number of cards on the first turn: {self.number_of_cards}")
                        self.check_cards(True)
                        self.log_callback(f"Hand state: {self.hand_state}")
                        for card in self.hand_state:
                            card_offset_x = card_offset_mapping.get(self.number_of_cards, 20)
                            start_x = self.card_start_x - (card["position"] * card_offset_x)
                            self.log_callback(f"Card: {card}")
                            if card.get("info", False) and card["info"]["level"] == 0 and not card["info"]["item_card"]:
                                # Log Pokémon being set as active
                                self.log_callback(f"🆕 Setting Active Pokémon: **{card['name']}**")
                                drag_position((start_x, self.card_y), (self.center_x, self.center_y))
                                self.active_pokemon.append(card)
                elif self.battle_actions.check_turn(self.turn_check_region, self.running) and self.active_pokemon:
                    # Log if it's the player's turn
                    self.log_callback("🔄 **Player's Turn**: Updating field and cards.")
                    self.update_field_and_hand_cards()
                    self.reset_view()

                    if self.number_of_cards and self.battle_actions.check_turn(self.turn_check_region, self.running):
                        self.play_turn()
                        self.try_attack()
                        self.end_turn()
                    time.sleep(1)

            ### GO TO MAIN SCREEN AFTER BATTLE
            time.sleep(1)
            if self.image_processor.check_and_click(
                screenshot, self.template_images["TAP_TO_PROCEED_BUTTON"], "Game ended"
            ):
                self.log_callback("🏁 **Game Over! Proceeding to next step...**")
                time.sleep(1)
            if self.image_processor.check_and_click(
                screenshot, self.template_images["TAP_TO_PROCEED_BUTTON"], "Game ended"
            ):
                time.sleep(1)
            if self.image_processor.check_and_click(
                screenshot, self.template_images["NEXT_BUTTON"], "Next button"
            ):
                time.sleep(1)
            if self.image_processor.check_and_click(
                screenshot, self.template_images["THANKS_BUTTON"], "Thanks button"
            ):
                time.sleep(2)
            self.image_processor.check_and_click(
                screenshot, self.template_images["CROSS_BUTTON"], "Cross button"
            )


    def play_turn(self):
        if not self.running:
            return False

        # Log the start of the turn
        self.log_callback("🎮 **Starting my turn...**")

        # Attempt to add energy to active Pokémon
        self.add_energy_to_pokemon()

        # Check for playable cards
        if 0 < len(self.hand_state) < 8:
            card_offset_x = card_offset_mapping.get(self.number_of_cards, 20)
            for card in self.hand_state:
                self.log_callback(f"{card['name']}")
                self.log_callback(f"{card["info"].get("item_card")}")
                # Check if the card is a trainer card and play it if possible
                if card["info"].get("item_card"):
                    start_x = self.card_start_x - (card["position"] * card_offset_x)
                    self.log_callback(f"🔹 Playing Trainer Card: **{card['name']}**")
                    drag_position((start_x, self.card_y), (self.center_x, self.center_y))
                    time.sleep(1)
                    drag_position((500, 1250), (self.center_x, self.center_y))
                    break

                # Check if we can play a basic Pokémon as the active Pokémon
                if not self.active_pokemon and card["info"]["level"] == 0:
                    start_x = self.card_start_x - (card["position"] * card_offset_x)
                    self.log_callback(f"🆕 Setting Active Pokémon: **{card['name']}**")
                    drag_position((start_x, self.card_y), (self.center_x, self.center_y))
                    self.active_pokemon.append(card)
                    time.sleep(1)
                    break

                # Check if we can play a basic Pokémon to the bench
                elif len(self.bench_pokemon) < 3:
                    if card["info"]["level"] == 0 and not card["info"]["item_card"]:
                        for bench_position in bench_positions:
                            self.reset_view()
                            time.sleep(1)
                            self.log_callback(
                                f"🪑 Adding **{card['name']}** to Bench Position {bench_positions.index(bench_position) + 1}"
                            )
                            drag_position((start_x, self.card_y), bench_position, 1.5)
                        self.bench_pokemon.append({
                            "name": card["name"].capitalize(),
                            "info": card["info"],
                            "energies": 0,
                        })
                        break

                # Check if the card can evolve the active Pokémon
                elif card["info"].get("evolves_from") and self.active_pokemon:
                    if (
                        card["info"]["evolves_from"].lower()
                        == self.active_pokemon[0]["name"].lower()
                    ):
                        start_x = self.card_start_x - (card["position"] * card_offset_x)
                        self.log_callback(
                            f"⬆️ Evolving **{self.active_pokemon[0]['name']}** to **{card['name']}**"
                        )
                        drag_position((start_x, self.card_y), (self.center_x, self.center_y))
                        self.active_pokemon[0] = {
                            "name": card["name"],
                            "info": card["info"],
                            "energies": self.active_pokemon[0].get("energies", 0),
                        }
                        time.sleep(1)
                        break

                # Check for the "Start Battle" button
                if self.image_processor.check_and_click(
                    take_screenshot(),
                    self.template_images["START_BATTLE_BUTTON"],
                    "Start battle button",
                ):
                    self.log_callback("⚔️ **Battle Start!**")
                    time.sleep(1)
                self.reset_view()

        else:
            # Default actions if no playable cards
            self.reset_view()
            self.add_energy_to_pokemon()
            self.try_attack()
            self.log_callback("🔥 **No playable cards found. Attempting an attack!**")


    def add_energy_to_pokemon(self):
        if not self.running:
            return False

        # Log the beginning of the energy addition process
        self.log_callback("🔋 Adding energy to active Pokémon...")

        # Perform the drag action to add energy
        drag_position((750, 1450), (self.center_x, self.center_y), 0.3)

        # Confirm energy was added
        self.log_callback("✅ Energy added to Pokémon.")

    def convert_api_card_data(self, card_data):
        # Convert stage to level
        stage = card_data.get("stage", "Basic")
        level_mapping = {"Basic": 0, "Stage 1": 1, "Stage 2": 2}

        # Calculate min energy requirement from attacks
        min_energies = 0  # Default to 0
        if card_data.get("attack") is None:
            card_data["attack"] = []

        if card_data["attack"]:  # Only calculate if there are attacks
            min_energies = float("inf")
            for attack in card_data["attack"]:
                info = attack.get("info", "")
                # Count energy symbols between curly braces
                if "{" in info and "}" in info:
                    energy_text = info[info.find("{") + 1 : info.find("}")]
                    energy_count = len(
                        energy_text
                    )  # Each character represents one energy
                    min_energies = min(min_energies, energy_count)

            # If we still have infinity, it means no valid energy counts were found
            if min_energies == float("inf"):
                min_energies = 0

        return {
            "level": level_mapping.get(stage, 0),
            "energies": min_energies,
            "evolves_from": card_data.get("prew_stage_name", None),
            "can_evolve": False,  # Will be updated when scanning deck
            "item_card": card_data.get("type", "").lower() in ["item", "supporter"],
            "id": card_data.get("id", None),
            "name": card_data.get("name", None),
            "number": card_data.get("number", None),
            "set_code": card_data.get("set_code", None),
            "set_name": card_data.get("set_name", None),
            "rarity": card_data.get("rarity", None),
            "color": card_data.get("color", None),
            "type": card_data.get("type", None),
            "slug": card_data.get("slug", None),
        }

    def check_cards(self, debug_images=False):
        if not self.running:
            return False

        # Begin checking hand cards
        self.log_callback("🃏 **Starting Hand Card Check** 🃏")
        x = self.card_start_x
        hand_cards = []
        self.hand_state.clear()

        for i in range(self.number_of_cards):
            if not self.running:
                break
            self.reset_view()

            # Capture and optionally save debug images of the card
            zoomed_card_image = self.battle_actions.get_card(x, self.card_y)
            if debug_images:
                debug_images_folder = "debug_images"
                os.makedirs(debug_images_folder, exist_ok=True)
                cv2.imwrite(f"{debug_images_folder}/{uuid.uuid4()}.png", zoomed_card_image)

            # Identify card by name
            card_name = self.battle_actions.identify_card(zoomed_card_image)
            selected_card = None
            selected_card_id = None

            if card_name is None:
                # Handle unknown card with user input
                self.log_callback("❓ Unidentified card. Requesting user input...")
                event = threading.Event()
                self.ui_instance.request_card_name(zoomed_card_image, event)

                if not event.wait(timeout=10):
                    self.log_callback("⚠️ Card identification timed out or was cancelled.")
                    continue

                card_name = self.ui_instance.card_name
                if not card_name:
                    self.log_callback("⚠️ Card identification cancelled by user.")
                    continue

                # Fetch card details from the API
                while True:
                    cards = self.card_data_manager.get_card_by_name(card_name)
                    if not cards:
                        event = threading.Event()
                        self.ui_instance.request_card_name(
                            zoomed_card_image, event, error_message=f"No matches for '{card_name}'. Please try again."
                        )
                        event.wait()
                        card_name = self.ui_instance.card_name
                        if not card_name:
                            break
                    elif len(cards) == 1:
                        selected_card = cards[0]
                        break
                    else:
                        # Find the best match among multiple options
                        similarities = [
                            (card, self.image_processor.calculate_similarity(
                                cv2.resize(
                                    cv2.imdecode(np.frombuffer(requests.get(self.card_data_manager.get_card_image_url(card["id"])).content, np.uint8), cv2.IMREAD_COLOR), (200, 300)
                                ), cv2.resize(zoomed_card_image, (200, 300))
                            )) for card in cards
                        ]
                        selected_card, _ = max(similarities, key=lambda x: x[1])
                        self.ui_instance.show_card_options(similarities, zoomed_card_image, event)
                        event.wait()
                        selected_card = self.ui_instance.selected_card
                        break

                selected_card_id = selected_card["id"] if selected_card else None
                self.log_callback(f"✨ Selected Card: {selected_card.get('name', 'Unknown')}")

                # Convert and store selected card data
                card_info = self.convert_api_card_data(selected_card)
                self.deck_info[selected_card["id"]] = card_info
                self.card_images[selected_card["id"]] = zoomed_card_image
                cv2.imwrite(f"images/cards/{selected_card['id']}.png", zoomed_card_image)
                for key, value in self.deck_info.items():
                    if value.get("evolves_from") == card_name:
                        self.deck_info[key]["can_evolve"] = True
                    if card_info.get("evolves_from") == value.get("name"):
                        card_info["can_evolve"] = True
                save_deck(self.deck_info)
            else:
                # Handle identified cards
                use_to_search = selected_card_id or card_name
                card_info = self.deck_info.get(use_to_search)
                if card_info is None:
                    self.log_callback(f"ℹ️ Card '{card_name}' not in deck, fetching from API...")
                    card = self.card_data_manager.get_card_by_id(card_name) or self.card_data_manager.get_card_by_name(card_name)
                    card_info = self.convert_api_card_data(card) if card else default_pokemon_stats
                    self.deck_info[card_name] = card_info
                    save_deck(self.deck_info)
                    self.log_callback(f"✨ Card '{card_name}' added to deck.")

            hand_cards.append(card_name.capitalize() if card_name else "Unknown Card")
            self.hand_state.append({"name": card_name, "position": i})

            # Move to next card position
            x -= card_offset_mapping.get(self.number_of_cards, 20)

        # Summarize the hand
        self.log_callback(f"🃏 **Current Hand:** {', '.join(hand_cards)}")
        for card in self.hand_state:
            self.log_callback(f"  • Position {card['position']}: {card['name']}")

    def click_bench_pokemons(self):
        # Take a screenshot to check for any interrupting UI elements
        screenshot = take_screenshot()

        # Check if any blocking element is visible
        if (
            not self.running
            or self.image_processor.check(screenshot, self.template_images["TAP_TO_PROCEED_BUTTON"], None)
            or self.image_processor.check(screenshot, self.template_images["NEXT_BUTTON"], None)
            or self.image_processor.check(screenshot, self.template_images["THANKS_BUTTON"], None)
            or self.image_processor.check(screenshot, self.template_images["BATTLE_BUTTON"], None)
            or self.image_processor.check(screenshot, self.template_images["CROSS_BUTTON"], None)
            or self.image_processor.check(screenshot, self.template_images["BATTLE_ALREADY_SCREEN"], None)
            or self.image_processor.check(screenshot, self.template_images["BATTLE_SCREEN"], None)
        ):
            self.log_callback("🛑 Action canceled: Blocking UI detected.")
            return False

        # Log start of clicking process
        self.log_callback("👉 Clicking each bench slot...")

        # Click on each bench position
        for index, bench_position in enumerate(bench_positions, start=1):
            click_position(bench_position[0], bench_position[1])
            self.log_callback(f"✅ Clicked Bench Slot {index}")

        # Confirm completion of clicks
        self.log_callback("✅ **All bench slots clicked**.")


    def check_field(self):
        if not self.running:
            return False

        # Starting the field check
        self.log_callback("🔍 **Field Check Initiated** 🔍")

        # Check for active Pokémon in the main zone
        self.check_active_pokemon()
        self.reset_view()

        # Logging active Pokémon status
        if self.active_pokemon:
            active = self.active_pokemon[0]
            self.log_callback(f"🌟 **Active Pokémon:** {active['name']}")
        else:
            self.log_callback("🌟 **No Active Pokémon in Main Zone**")

        # Checking bench Pokémon
        self.bench_pokemon = []
        self.log_callback("🪑 **Bench Pokémon Check** 🪑")

        for index, bench_position in enumerate(bench_positions):
            zoomed_card_image = self.battle_actions.get_card(bench_position[0], bench_position[1], 1.25)
            bench_zone_pokemon_name = self.battle_actions.identify_card(zoomed_card_image)

            if bench_zone_pokemon_name:
                self.log_callback(f"📍 Bench Slot {index + 1}: **{bench_zone_pokemon_name}**")

                # Add Pokémon to the bench list without extra details
                card_info = {
                    "name": bench_zone_pokemon_name,
                    "position": index,
                    "energies": 0,
                }
                self.bench_pokemon.append(card_info)
            else:
                self.log_callback(f"📍 Bench Slot {index + 1}: *Empty*")

            # Reset view for the next bench position check
            self.reset_view()
            time.sleep(0.25)

        # Bench summary
        if not self.bench_pokemon:
            self.log_callback("🚫 **No Pokémon on Bench**")
        else:
            self.log_callback("✅ **Bench Check Complete**")

    def check_active_pokemon(self):
        self.log_callback("Starting to check active Pokémon in main zone...")

        # Dragging to the center and capturing a zoomed card image
        drag_position((500, 1100), (self.center_x, self.center_y))
        self.log_callback("Dragged to main zone position to inspect active Pokémon.")

        # Capturing and identifying the card
        zoomed_card_image = self.battle_actions.get_card(self.center_x, self.center_y, 1.25)
        main_zone_pokemon_name = self.battle_actions.identify_card(zoomed_card_image)

        if main_zone_pokemon_name:
            self.log_callback(f"Identified Pokémon in main zone: {main_zone_pokemon_name}")

            # Initializing the active Pokémon list
            self.active_pokemon = []

            # Retrieving Pokémon details from the deck
            card_info = self.deck_info.get(main_zone_pokemon_name, default_pokemon_stats)
            card_info = {
                "name": main_zone_pokemon_name,
                "info": card_info,
                "energies": 0,
            }
            self.active_pokemon.append(card_info)

            # Logging detailed active Pokémon info
            self.log_callback(f"Active Pokémon details: {card_info}")
        else:
            # Clearing the active Pokémon if none is found
            self.active_pokemon = []
            self.log_callback("No active Pokémon found in main zone.")

    def reset_view(self):
        click_position(0, 1350)
        click_position(0, 1350)

    def check_n_cards(self):
        if not self.running:
            return False
        self.number_of_cards = None
        n_cards = self.battle_actions.check_number_of_cards(500, 1500)
        if n_cards:
            self.number_of_cards = int(n_cards)

    def update_field_and_hand_cards(self):
        if not self.running:
            return False
        self.try_attack()
        self.click_bench_pokemons()
        self.check_n_cards()
        self.reset_view()
        if self.number_of_cards:
            self.check_cards(True)
            self.reset_view()
            self.check_field()

    def end_turn(self):
        if not self.running:
            return False
        self.try_attack()
        self.reset_view()
        time.sleep(0.25)
        screenshot = take_screenshot()
        self.image_processor.check_and_click(
            screenshot, self.template_images["END_TURN"], "End turn"
        )
        time.sleep(0.25)
        self.image_processor.check_and_click(
            screenshot, self.template_images["OK"], "Ok"
        )

    def try_attack(self):
        self.add_energy_to_pokemon()

        # Flip a coin action
        drag_position((500, 1250), (self.center_x, self.center_y))

        time.sleep(0.25)
        self.reset_view()
        click_position(self.center_x, self.center_y)
        time.sleep(1)
        click_position(540, 1250)
        click_position(540, 1150)
        click_position(540, 1050)
        time.sleep(1)
        click_position(570, 1070)
        click_position(570, 1070)
        click_position(570, 1070)
        self.reset_view()

    def game_ended(self, screenshot):
        return self.image_processor.check(
            screenshot, self.template_images["TAP_TO_PROCEED_BUTTON"], "Game ended"
        )

    def next_step_available(self, screenshot):
        return (
            self.image_processor.check(
                screenshot, self.template_images["NEXT_BUTTON"], None
            )
            or self.image_processor.check(
                screenshot, self.template_images["THANKS_BUTTON"], None
            )
            or self.image_processor.check(
                screenshot, self.template_images["BATTLE_BUTTON"], None
            )
            or self.image_processor.check(
                screenshot, self.template_images["CROSS_BUTTON"], None
            )
            or self.image_processor.check(
                screenshot, self.template_images["BATTLE_ALREADY_SCREEN"], None
            )
            or self.image_processor.check(
                screenshot, self.template_images["BATTLE_SCREEN"], None
            )
        )

    def capture_region(self, region):
        cv2.imwrite(
            "screenshot_region.png", self.image_processor.capture_region(region)
        )
