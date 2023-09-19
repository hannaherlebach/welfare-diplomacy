"""
Charts for Same Policy experiments.
"""

import os

import matplotlib.pyplot as plt
import pandas as pd
import seaborn as sns

from chart_utils import (
    MODEL_NAME_TO_DISPLAY_NAME,
    MODEL_ORDER,
    MODEL_COMPARISON_COLORS,
    initialize_plot_bar,
    save_plot,
    get_results_full_path,
)

INPUT_FILES = [
    "../results/same_policy/SP Claude Both.csv",
    "../results/same_policy/SP GPT-3.5-Turbo-16K-0613.csv",
    "../results/same_policy/SP GPT-4-0613.csv",
    "../results/same_policy/SP GPT-4-Base.csv",
    "../results/same_policy/SP SuperExploiter (GPT-4).csv",
]
OUTPUT_DIR = "same_policy"


def main() -> None:
    """Main function."""

    # Load the data from each file into one big dataframe
    df = pd.concat([pd.read_csv(get_results_full_path(f)) for f in INPUT_FILES])

    # Change the agent model of all rows with non-empty super_exploiter_powers to "Super Exploiter"
    df.loc[df["super_exploiter_powers"].notnull(), "agent_model"] = "Super Exploiter"

    # Rename models based on MODEL_NAME_TO_DISPLAY_NAME
    df["agent_model"] = df["agent_model"].replace(MODEL_NAME_TO_DISPLAY_NAME)

    # Print how many runs there are for each agent_model
    print(f"Runs per agent_model:")
    print(df.groupby(["agent_model"]).size())

    # Print average _progress/percent_done for each agent_model
    print(f"Average _progress/percent_done per agent_model:")
    print(df.groupby(["agent_model"])["_progress/percent_done"].mean())

    # Plot a bunch of different bar graphs for different metrics
    for metric_name, y_label, improvement_sign, include_optimal, include_random in [
        ("benchmark/nash_social_welfare_global", "Nash Social Welfare", 1, True, True),
        ("benchmark/competence_score", "Competence Score", 1, False, False),
        ("combat/game_conflicts_avg", "Average Conflicts per Phase", -1, True, True),
    ]:
        # Initialize
        initialize_plot_bar()

        # Plot the welfare scores for each power
        cols_of_interest = [
            "agent_model",
            metric_name,
        ]

        plot_df = df[cols_of_interest].copy()

        # update the column names
        x_label = "Agent Model"
        plot_df.columns = [x_label, y_label]

        # Create the plot
        plot = sns.barplot(
            data=plot_df,
            x=x_label,
            y=y_label,
            errorbar="ci",
            order=MODEL_ORDER,
            capsize=0.2,
            palette=MODEL_COMPARISON_COLORS,
            # errwidth=2,
        )

        # Set labels and title
        plt.xlabel(x_label)
        y_axis_label = y_label
        if improvement_sign == 1:
            y_axis_label += " →"
        elif improvement_sign == -1:
            y_axis_label += " ←"
        plt.ylabel(y_axis_label)
        title = f"{y_label} by Agent Model"
        plt.title(title)

        # Save the plot
        output_file = get_results_full_path(
            os.path.join(OUTPUT_DIR, f"SP {y_label}.png")
        )
        save_plot(output_file)
        print(f"Saved plot '{title}' to {output_file}")

        # Clear the plot
        plt.clf()


if __name__ == "__main__":
    main()
