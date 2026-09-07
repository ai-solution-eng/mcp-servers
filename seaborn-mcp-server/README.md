# seaborn-mcp-server

## Disclosure
This MCP server was create with the help of [MiniMax M2.7](https://huggingface.co/MiniMaxAI/MiniMax-M2.7) using Opencode in HPE Private Cloude AI - It provides statistical visualization capabilities through the seaborn, matplotlib, and plotly libraries. It was created by giving MiniMax M2.7 the task to create an MCP server of the [Claude Seaborn Statistical Visualization Skill](https://mcpmarket.com/tools/skills/seaborn-statistical-visualization-5).

## Overview

The seaborn-mcp-server is a **Model Context Protocol (MCP) server** that exposes various data visualization tools/functions via the MCP protocol. It enables creating charts from tabular data with HTML output.

## Features

- **Multiple chart types**: scatter, line, bar, histogram, box, violin, KDE, pie, correlation heatmap, pair plots, faceted plots, and more
- **HTML output only**: Interactive Plotly HTML and seaborn/mpld3 HTML (no PNG/base64 images are produced)
- **Built-in statistics**: Automatic dataset profiling and statistical summaries
- **MCP protocol**: Works with any MCP-compatible client

## Installation

### Private Cloud AI deployment

Install the app into HPE Private Sloud AI using [helm chart](./deploy/helm/v.0.0.3/mcp-seaborn-server-0.0.3.tgz).

### Local deployment
```bash
# Local development
cd seaborn-mcp-server
pip install -r requirements.txt
python server.py
```

### Docker

```bash
cd deploy
docker build -t seaborn-mcp:latest .
docker run -p 9092:9092 seaborn-mcp:latest
```
The server runs on `http://0.0.0.0:9092` using streamable-http transport.


### Connect to Opencode

Within PCAI, add to opencode.json as follows. Use the internal and external endpoint URL.

```json
"seaborn": {
      "type": "remote",
      "url": "http://mcp-seaborn-server.mcp-seaborn-server.svc.cluster.local:9092/mcp"
    }
```

### Example Output

See [`department-performance.html`](./department-performance.html) for an example of charts which can be created using this MCP server. In this example, MiniMax M2.7 was used in Opencode in conjunction to an additional MCP server capable of executing SQL queries over a database of fictional students performance available in Private Cloud AI. This was the prompt:

```
You have access to a database of students performance in a biology degree course.

YOUR GOAL:
- create a comprehensive summary document `department-performance.htlm` which includes
- 5 graphs describing the performance of students, including for each course that you want to depict, how many student took the course, and among those how many failed or passed and how many did not take the course. You want also to depict other information like age distribution.
- each graph will be embedded in the summary document, and each graph will also have a description of the graph and what the data that is shown says.
- create the document step by step, create the first graph, embed in the document, describe it and save the document. Proceed with the next graph, updating the doc and so on. At the end, polish the document to make it ready for presentation to the principal of the school.
- In charts, ensure to use different colors for different classes or groups, to improve readibility. 

Use the tools available to you. Make sure you explicitely show your progress as you work on your tasks.
```

***Note:*** a problem which was encountered is that with multi-step tasks like this, the model may stop and requires you to prompt it to continue to work on its task. This behaviour could improved with better prompting.  

## Available Tools

### Core Utility Tools

| Tool | Purpose |
|------|---------|
| `health_check` | Verify server is running |
| `list_supported_plots` | Documentation on all chart types |
| `summarize_dataset` | Profile dataset (columns, dtypes, missing values, stats) |
| `create_plot` | Generic plotting with full request object |

### Visualization Tools

| Tool | Use Case |
|------|----------|
| `scatter_plot` | Relationships between two numeric variables |
| `line_plot` | Trends over time or ordered values |
| `histogram_plot` | Distribution of numeric variables |
| `kde_plot` | Smooth density curves |
| `box_plot` | Compare distributions and outliers |
| `violin_plot` | Full distribution shape comparison |
| `bar_plot` | Aggregated category comparisons |
| `count_plot` | Category frequency counts |
| `regression_plot` | Linear trend lines |
| `residual_plot` | Regression diagnostics |
| `pie_chart` | Part-to-whole composition |
| `correlation_heatmap` | Correlation matrix across numeric columns |
| `pair_plot` | All numeric pairs (scatter matrix) |
| `facet_plot` | Small multiples grid |

## Usage Example


###
```python
# Create a line plot for stock price trends
line_plot(
    data=[
        {"date": "2026-04-13", "close": 24.81},
        {"date": "2026-04-14", "close": 24.47},
        {"date": "2026-04-15", "close": 24.62},
        # ... more data
    ],
    x="date",
    y="close",
    title="HPE Stock Price Trend - Past Month"
)
```

### Common Parameters

All plotting functions accept:

| Parameter | Type | Description |
|-----------|------|-------------|
| `data` | `list[dict]` | Tabular data as JSON records |
| `x` | `string` | X-axis column name |
| `y` | `string` | Y-axis column name |
| `hue` | `string` (optional) | Color by category |
| `title` | `string` (optional) | Chart title |
| `max_return_rows` | `int` | Max rows to return (default: 500) |

### Output Format

Each tool returns JSON with:

```json
{
  "status": "ok",
  "kind": "line",
  "plot_spec": { /* plot configuration */ },
  "data_profile": { /* dataset profiling */ },
  "stats": { /* numeric summaries */ },
  "chart_json": { /* Vega-Lite spec for LLM reasoning */ },
  "html": {
    "plotly": "<interactive HTML>",
    "seaborn_mpld3": "<mpld3 HTML>"
  },
  "warnings": []
}
```


