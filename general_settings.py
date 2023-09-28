import glob
import os
import shutil
import subprocess
import sys
import warnings
from array import array
from collections import defaultdict, namedtuple
from copy import copy
from functools import partial, singledispatch
from itertools import chain, combinations, product
from pathlib import Path
from time import strftime

import joblib
import matplotlib.pyplot as plt
import numpy as np
import optuna
import pandas as pd
import plotly.express as px
import plotly.figure_factory as ff
import plotly.graph_objects as go
import scipy.stats as stats
import seaborn as sns
import shap
from colorama import Fore, Style
from IPython.core.display import HTML, display_html
from plotly.subplots import make_subplots
from scipy.cluster.hierarchy import linkage
from scipy.spatial.distance import squareform
from sklearn.base import BaseEstimator, OneToOneFeatureMixin, TransformerMixin

ON_KAGGLE = os.getenv("KAGGLE_KERNEL_RUN_TYPE") is not None

# Colorama settings.
CLR = (Style.BRIGHT + Fore.BLACK) if ON_KAGGLE else (Style.BRIGHT + Fore.WHITE)
RED = Style.BRIGHT + Fore.RED
BLUE = Style.BRIGHT + Fore.BLUE
CYAN = Style.BRIGHT + Fore.CYAN
RESET = Style.RESET_ALL

# Plots colors.
FONT_COLOR = "#4A4B52"
BACKGROUND_COLOR = "#FFFCFA"

# Data Frame color theme.
CELL_HOVER = {  # for row hover use <tr> instead of <td>
    "selector": "td:hover",
    "props": "background-color: #FFFCFA",
}
TEXT_HIGHLIGHT = {
    "selector": "td",
    "props": "color: #4A4B52; font-weight: bold",
}
INDEX_NAMES = {
    "selector": ".index_name",
    "props": "font-weight: normal; background-color: #FFFCFA; color: #4A4B52;",
}
HEADERS = {
    "selector": "th:not(.index_name)",
    "props": "font-weight: normal; background-color: #FFFCFA; color: #4A4B52;",
}
DF_STYLE = (INDEX_NAMES, HEADERS, TEXT_HIGHLIGHT)
DF_CMAP = sns.light_palette("#BAB8B8", as_cmap=True)


# Utility functions.
def download_from_kaggle(expr, directory=None, /) -> None:
    if directory is None:
        directory = Path("data")
    if not isinstance(directory, Path):
        raise TypeError("The `directory` argument must be `Path` instance!")
    match expr:
        case ["kaggle", _, "download", *args] if args:
            directory.parent.mkdir(parents=True, exist_ok=True)
            filename = args[-1].split("/")[-1] + ".zip"
            if not (directory / filename).is_file():
                subprocess.run(expr)
                shutil.unpack_archive(filename, directory)
                shutil.move(filename, directory)
        case _:
            raise SyntaxError("Invalid expression!")


def get_interpolated_colors(color1, color2, /, num_colors=2):
    """Return `num_colors` interpolated beetwen `color1` and `color2`.
    Arguments need to be HEX."""

    def interpolate(color1, color2, t):
        r1, g1, b1 = int(color1[1:3], 16), int(color1[3:5], 16), int(color1[5:7], 16)
        r2, g2, b2 = int(color2[1:3], 16), int(color2[3:5], 16), int(color2[5:7], 16)
        r = int(r1 + (r2 - r1) * t)
        g = int(g1 + (g2 - g1) * t)
        b = int(b1 + (b2 - b1) * t)
        return f"#{r:02X}{g:02X}{b:02X}"

    return [interpolate(color1, color2, k / (num_colors + 1)) for k in range(1, num_colors + 1)]


def get_pretty_frame(frame, /, gradient=False, formatter=None, precision=3, repr_html=False):
    stylish_frame = frame.style.set_table_styles(DF_STYLE).format(
        formatter=formatter, precision=precision
    )
    if gradient:
        stylish_frame = stylish_frame.background_gradient(DF_CMAP)  # type: ignore
    if repr_html:
        stylish_frame = stylish_frame.set_table_attributes("style='display:inline'")._repr_html_()
    return stylish_frame


def numeric_descr(frame, /):
    return (
        frame.describe(percentiles=(0.01, 0.05, 0.25, 0.50, 0.75, 0.95, 0.99))
        .T.drop("count", axis=1)
        .rename(columns=str.title)
    )


def missing_unique_vals_summary(frame, /):
    missing_vals = frame.isna().sum()
    missing_vals_ratio = missing_vals / len(frame)
    unique_vals = frame.apply(lambda col: len(col.unique()))
    most_freq_count = frame.apply(lambda col: col.value_counts().iloc[0])
    most_freq_val = frame.mode().iloc[:1].T.squeeze()
    unique_ratio = unique_vals / len(frame)
    freq_count_ratio = most_freq_count / len(frame)

    return pd.DataFrame(
        {
            "Dtype": frame.dtypes,
            "MissingValues": missing_vals,
            "MissingValuesRatio": missing_vals_ratio,
            "UniqueValues": unique_vals,
            "UniqueValuesRatio": unique_ratio,
            "MostFreqValue": most_freq_val,
            "MostFreqValueCount": most_freq_count,
            "MostFreqValueCountRatio": freq_count_ratio,
        }
    )


def check_categories_alignment(train, test, /):
    print(CLR + "The same categories in training and test datasets?\n")
    cat_features = test.select_dtypes(include="object").columns.to_list()

    for feature in cat_features:
        train_unique = set(train[feature].unique())
        test_unique = set(test[feature].unique())
        same = np.all(train_unique == test_unique)
        print(CLR + f"{feature:25s}", BLUE + f"{same}")


def get_n_rows_and_axes(n_features, n_cols):
    n_rows = int(np.ceil(n_features / n_cols))
    current_col = range(1, n_cols + 1)
    current_row = range(1, n_rows + 1)
    return n_rows, list(product(current_row, current_col))


def get_distributions_figure(features, train, test, /, **kwargs):
    histnorm = kwargs.get("histnorm", "probability density")
    train_color = kwargs.get("train_color", "blue")
    test_color = kwargs.get("test_color", "red")
    n_cols = kwargs.get("n_cols", 3)
    n_rows, axes = get_n_rows_and_axes(len(features), n_cols)

    fig = make_subplots(
        rows=n_rows,
        cols=n_cols,
        y_title=histnorm.title(),
        horizontal_spacing=kwargs.get("horizontal_spacing", 0.1),
        vertical_spacing=kwargs.get("vertical_spacing", 0.1),
    )
    fig.update_annotations(font_size=kwargs.get("annotations_font_size", 14))

    for frame, color, name in zip((train, test), (train_color, test_color), ("Train", "Test")):
        if frame is None:  # Test dataset may not exist.
            break

        for k, (var, (row, col)) in enumerate(zip(features, axes), start=1):
            # density, bins = np.histogram(frame[var].dropna(), density=True)
            fig.add_histogram(
                x=frame[var],
                histnorm=histnorm,
                marker_color=color,
                marker_line_width=0,
                opacity=0.75,
                name=name,
                legendgroup=name,
                showlegend=k == 1,
                row=row,
                col=col,
            )
            fig.update_xaxes(title_text=var, row=row, col=col)

    fig.update_xaxes(
        tickfont_size=8, showgrid=False, titlefont_size=8, titlefont_family="Arial Black"
    )
    fig.update_yaxes(tickfont_size=8, showgrid=False)

    fig.update_layout(
        width=840,
        height=kwargs.get("height", 640),
        title=kwargs.get("title", "Distributions"),
        font_color=FONT_COLOR,
        title_font_size=18,
        plot_bgcolor=BACKGROUND_COLOR,
        paper_bgcolor=BACKGROUND_COLOR,
        bargap=kwargs.get("bargap", 0),
        bargroupgap=kwargs.get("bargroupgap", 0),
        legend=dict(yanchor="bottom", xanchor="right", y=1, x=1, orientation="h", title=""),
    )
    return fig


class Chi2ContingencyImputer(BaseEstimator, TransformerMixin, OneToOneFeatureMixin):
    def __init__(self, cols_to_ignore=None):
        self.cols_to_ignore = cols_to_ignore

    def fit(self, X, y=None):
        cat_features = X.select_dtypes(include="object").columns.tolist()
        if self.cols_to_ignore is not None:
            cat_features = np.setdiff1d(cat_features, self.cols_to_ignore)

        related_vars = self._calculate_most_related_variables(X, cat_features)
        all_modes = self._get_all_modes(X, related_vars)

        self.related_vars_ = related_vars
        self.all_modes_ = all_modes
        return self

    def transform(self, X, y=None):
        Xc = X.copy()

        for var1, var2, modes in self.all_modes_:
            # On var2 because we grouped by var2 earlier in `_get_all_modes()`.
            Xc = Xc.merge(modes, on=var2, how="left")  # type: ignore
            Xc[var1] = Xc[var1].fillna(Xc[f"Mode_{var1}"])
            Xc = Xc.drop(f"Mode_{var1}", axis=1)

        Xc.index = X.index
        return Xc

    def _calculate_most_related_variables(self, X, cat_features):
        results = []
        # Calculate whether there is relation between categorical feature pairs.
        for var1, var2 in list(product(cat_features, cat_features)):
            if var1 == var2:
                continue
            contingency = pd.crosstab(X[var1], X[var2])
            result = stats.chi2_contingency(contingency)
            pvalue = result.pvalue  # type: ignore
            results.append((var1, var2, pvalue))

        # For each feature, determine with which other one, there is the strongest relation.
        related_vars = pd.DataFrame(results, columns=["var1", "var2", "pvalue"])
        min_ids = related_vars.groupby("var1")["pvalue"].idxmin()  # Determine minimal p-value.
        return related_vars.iloc[min_ids, :2].to_numpy()  # Most related pairs as 2D array.

    def _get_all_modes(self, X, related_vars):
        all_modes = []
        # For each most related pair, group one feature and determine
        # the most frequent value of the second feature in that group.
        for var1, var2 in related_vars:  # type: ignore
            all_modes.append(
                (
                    var1,
                    var2,
                    (
                        X.groupby(var2, dropna=False)[var1]  # type: ignore
                        .agg(lambda g: g.mode().iloc[0] if not g.mode().empty else None)
                        .reset_index()
                        .rename(columns={var1: f"Mode_{var1}"})  # Avoid the same name.
                    ),
                )
            )
        return all_modes  # Feature pairs with series mapper.


# Html highlight. Must be included at the end of all imports!
HTML(
    """
<style>
code {
    background: rgba(42, 53, 125, 0.10) !important;
    border-radius: 4px !important;
}
a {
    color: rgba(123, 171, 237, 1.0) !important;
}
ol.numbered-list {
  counter-reset: item;
}
ol.numbered-list li {
  display: block;
}
ol.numbered-list li:before {
  content: counters(item, '.') '. ';
  counter-increment: item;
}
</style>
"""
)
