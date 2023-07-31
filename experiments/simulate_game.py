"""
Language model scaffolding to play Diplomacy.


"""

import argparse
import logging
import os

from diplomacy import Game, GamePhaseData, Message, Power
from diplomacy.utils.export import to_saved_game_format
import numpy as np
from tqdm import tqdm
import wandb

from backends import ModelResponse
import constants
import utils
from prompter import Prompter, model_name_to_prompter


def main():
    """Simulate a game of Diplomacy with the given parameters."""
    # Parse args
    args = parse_args()

    # Initialize seed, wandb, game, logger, and prompter
    utils.set_seed(args.seed)

    wandb.init(
        entity=args.entity,
        project=args.project,
        save_code=True,
        config=vars(args),
        mode="disabled" if args.disable_wandb else "online",
        settings=wandb.Settings(code_dir="experiments"),
    )
    assert wandb.run is not None
    game: Game = Game(map_name=args.map_name)
    logger = logging.getLogger(__name__)
    logging.basicConfig()
    logger.setLevel(args.log_level)

    prompter: Prompter = model_name_to_prompter(
        args.model,
        temperature=args.temperature,
        top_p=args.top_p,
    )

    utils.log_info(
        logger,
        f"Starting game with map {args.map_name} and model {args.model} ending after {args.max_years} years with {args.max_message_rounds} message rounds.",
    )

    # Log the initial state of the game
    rendered_with_orders = game.render(incl_abbrev=True)
    log_object = {  # TODO add other things from below
        "meta/year_fractional": 0.0,
        "board/rendering_with_orders": wandb.Html(rendered_with_orders),
        "board/rendering_state": wandb.Html(rendered_with_orders),
    }
    for power in game.powers.values():
        short_name = power.name[:3]
        if game.phase_type == "A":
            log_object[f"score/units/{short_name}"] = len(power.units)
            log_object[f"score/welfare/{short_name}"] = power.welfare_points
        else:
            log_object[f"score/centers/{short_name}"] = len(power.centers)

    wandb.log(log_object)

    simulation_max_years = (
        args.early_stop_max_years if args.early_stop_max_years > 0 else args.max_years
    )

    progress_bar_phase = tqdm(total=simulation_max_years * 3, desc="🔄️ Phases")
    while not game.is_game_done:
        utils.log_info(logger, f"🕰️  Beginning phase {game.get_current_phase()}")

        # Cache the list of possible orders for all locations
        possible_orders = game.get_all_possible_orders()

        total_num_orders = 0
        total_num_valid_orders = 0
        list_valid_order_ratios = []
        total_message_sent = 0
        prompter_response_history: list[tuple[str, int, ModelResponse]] = []
        list_completion_times_sec = []
        list_prompt_tokens = []
        list_completion_tokens = []
        list_total_tokens = []

        # During Retreats, only 1 round of completions without press
        num_of_message_rounds = args.max_message_rounds if game.phase_type != "R" else 1
        num_completing_powers = (
            len(game.powers)
            if game.phase_type != "R"
            else len([power for power in game.powers.values() if power.retreats])
        )
        progress_bar_messages = tqdm(
            total=num_of_message_rounds * num_completing_powers, desc="🙊 Messages"
        )
        for message_round in range(num_of_message_rounds):
            utils.log_info(
                logger,
                f"📨 Beginning message round {message_round}/{num_of_message_rounds}",
            )

            # Randomize order of powers
            power_names = list(game.powers.items())
            np.random.shuffle(power_names)

            count_completions_one_round = 0
            power: Power
            for power_name, power in power_names:
                # On retreat phases, skip powers that have no retreats to make
                if game.phase_type == "R" and not power.retreats:
                    continue

                # Prompting the model for a response
                prompter_response = prompter.respond(
                    power,
                    game,
                    possible_orders,
                    message_round,
                    args.max_message_rounds,
                    args.max_years + 1900,
                )
                list_completion_times_sec.append(prompter_response.completion_time_sec)
                list_prompt_tokens.append(prompter_response.prompt_tokens)
                list_completion_tokens.append(prompter_response.completion_tokens)
                list_total_tokens.append(prompter_response.total_tokens)
                prompter_response_history.append(
                    (power_name, message_round, prompter_response)
                )
                if game.phase_type == "R":
                    assert (
                        len(prompter_response.messages) == 0
                    ), "No messages are allowed during retreats."
                count_completions_one_round += 1
                utils.log_info(
                    logger,
                    f"⚙️  {power_name} {game.get_current_phase()} Round {message_round}: Prompter {prompter_response.model_name} took {prompter_response.completion_time_sec:.2f}s to respond.\nReasoning: {prompter_response.reasoning}\nOrders: {prompter_response.orders}\nMessages: {prompter_response.messages}",
                )

                # Check how many of the orders were valid
                num_valid_orders = 0
                for order in prompter_response.orders:
                    if "WAIVE" in order or "VOID" in order:
                        utils.log_warning(
                            logger,
                            f"Order '{order}' should not be generated by prompter",
                        )
                        num_valid_orders += 1
                        continue
                    word = order.split()
                    location = word[1]
                    if (
                        location in possible_orders
                        and order in possible_orders[location]
                    ):
                        num_valid_orders += 1
                num_orders = len(prompter_response.orders)
                valid_order_ratio = 1.0
                if num_orders > 0:
                    valid_order_ratio = num_valid_orders / num_orders
                utils.log_info(
                    logger,
                    f"✔️  {power_name} valid orders: {num_valid_orders}/{num_orders} = {valid_order_ratio * 100.0:.2f}%",
                )
                total_num_orders += num_orders
                total_num_valid_orders += num_valid_orders
                list_valid_order_ratios.append(valid_order_ratio)

                # Set orders, clearing first due to multiple message rounds
                game.set_orders(power_name, [])
                game.set_orders(power_name, prompter_response.orders)

                # Send messages
                for recipient, message in prompter_response.messages.items():
                    game.add_message(
                        Message(
                            sender=power_name,
                            recipient=recipient,
                            message=message,
                            phase=game.get_current_phase(),
                        )
                    )
                    total_message_sent += 1

                progress_bar_messages.update(1)

        # Render saved orders before processing
        rendered_with_orders = game.render(incl_abbrev=True)

        # Processing the game to move to the next phase
        game.process()

        # Check whether to end the game
        if int(game.phase.split()[1]) - 1900 > simulation_max_years:
            game._finish([])

        # Log to Weights & Biases
        phase: GamePhaseData = game.get_phase_history()[-1]
        rendered_state = game.render(incl_abbrev=True)
        model_response_table = wandb.Table(
            columns=[
                "phase",
                "power",
                "round",
                "model",
                "reasoning",
                "orders",
                "messages",
                "system_prompt",
                "user_prompt",
            ],
            data=[
                [
                    phase.name,
                    power_name,
                    response_message_round,
                    prompter_response.model_name,
                    prompter_response.reasoning,
                    prompter_response.orders,
                    [
                        f"{power_name} -> {recipient}: {message}"
                        for recipient, message in prompter_response.messages.items()
                    ],
                    prompter_response.system_prompt,
                    prompter_response.user_prompt,
                ]
                for power_name, response_message_round, prompter_response in prompter_response_history
            ],
        )
        valid_order_total_avg = 1.0
        if total_num_orders > 0:
            valid_order_total_avg = total_num_valid_orders / total_num_orders
        log_object = {
            "meta/year_fractional": utils.get_game_fractional_year(phase),
            "board/rendering_with_orders": wandb.Html(rendered_with_orders),
            "board/rendering_state": wandb.Html(rendered_state),
            "orders/num_total": total_num_orders,
            "orders/num_valid": total_num_valid_orders,
            "orders/valid_ratio_total_avg": valid_order_total_avg,
            "orders/valid_ratio_avg_avg": np.mean(list_valid_order_ratios),
            "messages/num_total": total_message_sent,
            "messages/num_avg": total_message_sent / count_completions_one_round,
            "model/completion_time_sec_avg": np.mean(list_completion_times_sec),
            "model/response_table": model_response_table,
            "tokens/prompt_tokens_avg": np.mean(list_prompt_tokens),
            "tokens/completion_tokens_avg": np.mean(list_completion_tokens),
            "tokens/total_tokens_avg": np.mean(list_total_tokens),
            "tokens/prompt_tokens_min": np.min(list_prompt_tokens),
            "tokens/completion_tokens_min": np.min(list_completion_tokens),
            "tokens/total_tokens_min": np.min(list_total_tokens),
            "tokens/prompt_tokens_max": np.max(list_prompt_tokens),
            "tokens/completion_tokens_max": np.max(list_completion_tokens),
            "tokens/total_tokens_max": np.max(list_total_tokens),
            "tokens/prompt_tokens_median": np.median(list_prompt_tokens),
            "tokens/completion_tokens_median": np.median(list_completion_tokens),
            "tokens/total_tokens_median": np.median(list_total_tokens),
            "tokens/prompt_tokens_hist": wandb.Histogram(list_prompt_tokens),
            "tokens/completion_tokens_hist": wandb.Histogram(list_completion_tokens),
            "tokens/total_tokens_hist": wandb.Histogram(list_total_tokens),
        }

        for power in game.powers.values():
            short_name = power.name[:3]
            if phase.name[-1] == "A" or phase.name[-1] == "R":
                # Centers/welfare/units only change after adjustments or sometimes retreats
                log_object[f"score/units/{short_name}"] = len(power.units)
                log_object[f"score/welfare/{short_name}"] = power.welfare_points
                log_object[f"score/centers/{short_name}"] = len(power.centers)

        if phase.name[-1] == "A":
            # Track metrics of aggregated welfare
            welfare_list = [power.welfare_points for power in game.powers.values()]
            log_object["welfare/hist"] = wandb.Histogram(welfare_list)
            log_object["welfare/min"] = np.min(welfare_list)
            log_object["welfare/max"] = np.max(welfare_list)
            log_object["welfare/mean"] = np.mean(welfare_list)
            log_object["welfare/median"] = np.median(welfare_list)
            log_object["welfare/total"] = np.sum(welfare_list)

        wandb.log(log_object)

        # Print some information about the game
        score_string = " ".join(
            [
                f"{power.abbrev}: {len(power.centers)}/{len(power.units)}/{power.welfare_points}"
                for power in game.powers.values()
            ]
        )
        utils.log_info(logger, f"📊 {phase.name} SC/UN/WP: {score_string}")

        # Update the progress bar based on how many turns have progressed (just counting M and A)
        new_phase_type = game.phase_type
        if new_phase_type == "M":
            # Any to M, update 1
            progress_bar_phase.update(1)
        elif new_phase_type == "A":
            # M or R to A, update 1
            progress_bar_phase.update(1)
        elif new_phase_type == "R":
            # Retreats, don't count it
            pass
        else:
            raise ValueError(f"Unknown phase type {new_phase_type}")

    # Exporting the game to disk to visualize (game is appended to file)
    # Alternatively, we can do >> file.write(json.dumps(to_saved_game_format(game)))
    if not args.no_save:
        if not os.path.exists(args.output_folder):
            os.makedirs(args.output_folder)
        output_id = "debug" if args.disable_wandb else wandb.run.id
        to_saved_game_format(
            game, output_path=os.path.join(args.output_folder, f"game-{output_id}.json")
        )


def parse_args():
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Simulate a game of Diplomacy with the given parameters.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--log_level", dest="log_level", default="INFO", help="🪵 Logging level."
    )
    parser.add_argument(
        "--map",
        dest="map_name",
        default="standard_welfare",
        help="🗺️ Map name which switches between rulesets.",
    )
    parser.add_argument(
        "--output_folder",
        dest="output_folder",
        default="games",
        help="📁Folder to save the game to.",
    )
    parser.add_argument(
        "--no_save",
        dest="no_save",
        action="store_true",
        help="💾Don't save the game to disk.",
    )
    parser.add_argument("--seed", dest="seed", type=int, default=0, help="Random seed")
    parser.add_argument(
        "--entity",
        dest="entity",
        default=None,
        help="👤Weights & Biases entity name (can be your username). Note you can also use the WANDB_ENTITY env var.",
    )
    parser.add_argument(
        "--project",
        dest="project",
        default=constants.WANDB_PROJECT,
        help="📝Weights & Biases project name.",
    )
    parser.add_argument(
        "--disable_wandb",
        dest="disable_wandb",
        action="store_true",
        help="⚖️ Disable logging to wandb.",
    )
    parser.add_argument(
        "--max_years",
        dest="max_years",
        type=int,
        default=10,
        help="🗓️ Ends the game after this many years (3x as many turns).",
    )
    parser.add_argument(
        "--early_stop_max_years",
        dest="early_stop_max_years",
        type=int,
        default=0,
        help="⏱️ Early stop while telling the models the game lasts --max_years long. No effect if 0.",
    )
    parser.add_argument(
        "--max_message_rounds",
        dest="max_message_rounds",
        type=int,
        default=3,
        help="📨Max rounds of messaging per turn.",
    )
    parser.add_argument(
        "--model",
        dest="model",
        default="gpt-4-32k-0613",
        help="🤖Model name to use. Can be an OpenAI Chat model, 'random', or 'retreats' (contrive a retreat situation).",
    )
    parser.add_argument(
        "--temperature",
        dest="temperature",
        type=float,
        default=0.3,
        help="🌡️ Sampling temperature.",
    )
    parser.add_argument(
        "--top_p",
        dest="top_p",
        type=float,
        default=0.9,
        help="⚛️ Top-p for nucleus sampling.",
    )

    args = parser.parse_args()
    return args


if __name__ == "__main__":
    main()
