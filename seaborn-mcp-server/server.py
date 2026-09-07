from mcp_instance import mcp

from tools.core import (
    health_check,
    list_supported_plots,
    summarize_dataset,
    create_plot,
)
from tools.plots_1d import histogram_plot, kde_plot, box_plot, violin_plot, count_plot
from tools.plots_2d import (
    scatter_plot,
    line_plot,
    bar_plot,
    regression_plot,
    residual_plot,
    pie_chart,
    correlation_heatmap,
    pair_plot,
    facet_plot,
)

if __name__ == "__main__":
    mcp.run(transport="streamable-http", host="0.0.0.0", port=9092)