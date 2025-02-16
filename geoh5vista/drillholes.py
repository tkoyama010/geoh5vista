"""Methods to convert line set objects to VTK data objects"""


__all__ = [
    "line_set_to_vtk",
]

__displayname__ = "Line Set"

import numpy as np
import pyvista

from geoh5vista.utilities import add_data


def line_set_to_vtk(lse, origin=(0.0, 0.0, 0.0)):
    """Convert the line set to a :class:`pyvista.PolyData` data object.

    Args:
        lse (:class:`omf.lineset.LineSetElement`): The line set to convert

    Return:
        :class:`pyvista.PolyData`
    """
    ids = np.array(lse.geometry.segments).reshape(-1, 2).astype(np.int_)
    lines = np.c_[np.full(len(ids), 2, dtype=np.int_), ids]

    output = pyvista.PolyData()
    output.points = np.array(lse.geometry.vertices)
    output.lines = lines

    indices = output.connectivity().cell_data["RegionId"]
    output["Line Index"] = indices

    # Now add data to lines:
    add_data(output, lse.data)

    # TODO: if subtype is borehole make a tube

    output.points += np.array(origin)
    return output


line_set_to_vtk.__displayname__ = "Line Set to VTK"


import collections.abc
import pyvista as pv
from geoh5py.workspace import Workspace
from geoh5py.groups import DrillholeGroup
from geoh5py.objects import Drillhole
import numpy as np
import pandas as pd
import distinctipy
from matplotlib.colors import ListedColormap

from .plotter import Plotter
from .drill_log import DrillLog
from .utils import (
    convert_to_numpy_array,
    convert_array_type,
    encode_categorical_data,
    make_categorical_cmap,
    make_color_map_fractional,
)


class HoleData:
    """Base class for storing and desurveying drill hole data. Used by `DrillDown.Points` and `DrillDown.Intervals`."""

    def __init__(self):
        self.hole_ids = []
        self.array_names_all = []
        self.categorical_array_names = []
        self.continuous_array_names = []
        self._depths = None
        self.data = {}
        self.image_array_names = []
        self.cat_to_color_map = {}
        self.code_to_cat_map = {}
        self.cat_to_code_map = {}
        self.matplotlib_formatted_color_maps = {}
        self.categorical_color_rng = 999
        self.categorical_pastel_factor = 0.2

    def add_data(
        self,
        hole_ids,
        depths,
        data,
        array_names=[],
        image_array_names=[],
        return_data=False,
        construct_categorical_cmap=False,
    ):
        """Add drill hole data.

        Parameters
        ----------
        hole_ids : array-like
            Hole IDs as 1D array with length equal to the number of data points.

        depths : array-like
            Depths along drillhole path as 1D array with length equal to the number of data points.

        data : array-like or pandas DataFrame
            Matrix of drillhole data to be added. If a pandas DataFrame is passed, the column names will be used as the array names.

        array_names : list of str, optional
            Names of the arrays in the data matrix. Required if `data` is not a pandas DataFrame.

        image_array_names : list of str, optional
            Names of the image arrays (i.e., containing paths to tiled images) in the data matrix if such arrays are present.

        return_data : bool, optional
            If True, returns the data.

        construct_categorical_cmap : bool, optional
            If True, constructs a categorical color map for the categorical array names upon displaying in Downhole plot or Plotter.

        Examples
        --------
        >>> import drilldown as dd
        >>> data_dict = dd.examples.load_tom_zone_macpass_project()
        >>> collar_data = data_dict["collar"]
        >>> survey_data = data_dict["survey"]
        >>> assay_data = data_dict["assay"]
        >>> collars = dd.Collars()
        >>> collars.add_data(collar_data["hole_ID"], collar_data[["x", "y", "z"]])
        >>> surveys = dd.Surveys()
        >>> surveys.add_data(survey_data["hole_ID"], survey_data["depth"], survey_data["azimuth"], survey_data["dip"])
        >>> surveys.locate(collars)
        >>> assays = dd.Intervals()
        >>> assays.add_data(assay_data["hole_ID"], assay_data[["depth_from", "depth_to"]], assay_data)
        >>> assays.desurvey(surveys)
        >>> assays.show()

        Returns
        -------
        dict, optional
            If `return_data` is True, returns the data.

        """
        # save flag to construct categorical color map
        self.construct_categorical_cmap = construct_categorical_cmap

        # add hole IDs
        hole_ids = convert_to_numpy_array(hole_ids)
        self.hole_ids = hole_ids
        self.unique_hole_ids = np.unique(hole_ids)

        # encode hole IDs, as strings are wiped in pyvista meshes
        hole_ids_encoded, hole_ids_unique = pd.factorize(hole_ids)
        self.cat_to_code_map["hole ID"] = {
            hole_id: code for code, hole_id in enumerate(hole_ids_unique)
        }
        self.code_to_cat_map["hole ID"] = {
            code: hole_id for code, hole_id in enumerate(hole_ids_unique)
        }
        self.categorical_array_names.append("hole ID")

        # add from-to depths
        depths = convert_to_numpy_array(depths)

        ## remove data corresponding to NaN depths
        if depths.ndim > 1:
            nan_bool = np.any(np.isnan(depths), axis=1)
        else:
            nan_bool = np.isnan(depths)

        if np.any(nan_bool):
            depths = depths[~nan_bool]
            self.hole_ids = self.hole_ids[~nan_bool]
            data = data[~nan_bool]

        self.depths = depths

        # add data
        if isinstance(data, pd.core.frame.DataFrame):
            array_names = data.columns.tolist()
            self.array_names_all += array_names
            data = data.values

        else:
            if len(array_names) == 0:
                raise ValueError("Array names must be provided.")

        data = convert_to_numpy_array(data, collapse_dim=False)
        for dataset, array_name in zip(data.T, array_names):
            dataset, _type = convert_array_type(dataset, return_type=True)

            if _type == "str":  # categorical data
                self.categorical_array_names.append(array_name)

                # encode categorical data
                code_to_cat_map, dataset = encode_categorical_data(dataset)
                self.code_to_cat_map[array_name] = code_to_cat_map
                self.cat_to_code_map[array_name] = {
                    cat: code for code, cat in code_to_cat_map.items()
                }

            else:
                self.continuous_array_names.append(array_name)

            self.data[array_name] = {
                "values": dataset,
                "type": _type,
            }

        self.image_array_names = image_array_names

        if return_data == True:
            return self.data

    def _construct_categorical_cmap(
        self, array_names=[], cycle=True, rng=999, pastel_factor=0.2
    ):
        """Construct categorical color map for categorical array names.

        Parameters
        ----------
        array_names : list of str, optional
            Names of the categorical arrays for which to construct the categorical color map. If not provided, constructs the color map for all categorical array names.

        cycle : bool, optional
            If True, cycles through matplotlib colors to construct the categorical color map. If False, uses distinctipy to select n distinct colors, where n is the number of categories present in a given array.

        rng : int, optional
            Random number generator seed for distinctipy.

        pastel_factor : float, optional
            Factor by which to increase the pastelness of the colors. Only used by distinctpy (i.e., if cycle is True).

        """

        if len(array_names) == 0:
            array_names = [
                array_name
                for array_name in self.categorical_array_names
                if array_name not in self.cat_to_color_map.keys()
            ]

        for array_name in array_names:
            categories = self.cat_to_code_map[array_name].keys()
            cat_to_color_map, matplotlib_formatted_color_maps = make_categorical_cmap(
                categories, cycle=cycle, rng=rng, pastel_factor=pastel_factor
            )

            self.cat_to_color_map[array_name] = cat_to_color_map
            self.matplotlib_formatted_color_maps[array_name] = (
                matplotlib_formatted_color_maps
            )

    def add_categorical_cmap(self, array_name, cmap=None, cycle=True):
        """Add categorical color map for a given array name.

        Parameters
        ----------
        array_name : str
            Name of the categorical array for which a categorical color map is added.

        cmap : dict, optional
            Dictionary with keys as categories and values as colors in any format accepted by PyVista. If not provided, the color map is constructed using `self._construct_categorical_cmap`.

        cycle : bool, optional
            If no cmap is passed, cycle is passed to `self._construct_categorical_cmap`. If True, cycles through matplotlib colors to construct the categorical color map. If False, uses distinctipy to select n distinct colors, where n is the number of categories present in a given array.

        Examples
        --------
        >>> import drilldown as dd
        >>> data_dict = dd.examples.load_tom_zone_macpass_project()
        >>> collar_data = data_dict["collar"]
        >>> survey_data = data_dict["survey"]
        >>> interpretation_data = data_dict["interpretation"]
        >>> collars = dd.Collars()
        >>> collars.add_data(collar_data["hole_ID"], collar_data[["x", "y", "z"]])
        >>> surveys = dd.Surveys()
        >>> surveys.add_data(survey_data["hole_ID"], survey_data["depth"], survey_data["azimuth"], survey_data["dip"])
        >>> surveys.locate(collars)
        >>> interp = dd.Intervals()
        >>> interp.add_data(interpretation_data["hole_ID"], interpretation_data[["from", "to"]], interpretation_data)
        >>> interp.desurvey(surveys)
        >>> interp.add_categorical_cmap("code", cycle=False)
        >>> interp.show()
        """

        if array_name not in self.categorical_array_names:
            raise ValueError(f"Data for {array_name} not present.")

        if cmap is None:
            self._construct_categorical_cmap([array_name], cycle=cycle)
        else:
            if not isinstance(cmap, dict):
                raise TypeError("Categorical color map must be a dictionary.")

            # ensure categorical color map colors are fractional
            cmap = make_color_map_fractional(cmap)

            colors = [cmap[cat] for cat in cmap.keys()]
            n_missing_colors = len(self.cat_to_code_map[array_name].keys()) - len(
                colors
            )
            colors = distinctipy.get_colors(
                n_missing_colors,
                exclude_colors=colors,
                return_excluded=True,
                pastel_factor=self.categorical_pastel_factor,
                rng=self.categorical_color_rng,
            )

            # create categorical color map
            categories = [cat for cat in cmap.keys()] + [
                cat
                for cat in self.cat_to_code_map[array_name].keys()
                if cat not in cmap.keys()
            ]
            self.cat_to_color_map[array_name] = {
                cat: color for cat, color in zip(categories, colors)
            }

            # create encoded categorical color map
            codes = [self.cat_to_code_map[array_name][cat] for cat in categories]

            # create matplotlib categorical color map
            codes.sort()
            self.matplotlib_formatted_color_maps[array_name] = ListedColormap(
                [
                    self.cat_to_color_map[array_name][
                        self.code_to_cat_map[array_name][code]
                    ]
                    for code in codes
                ]
            )

    @property
    def depths(self):
        """Return depths along the drill hole paths.

        Returns
        -------
        array-like
            Depths along the drill hole paths.

        """
        return self._depths

    @depths.setter
    def depths(self, depths):
        """Set depths along the drill hole paths.

        Should only be set within the `self.add_data` method.
        """

        if depths is not None:
            depths = convert_to_numpy_array(depths)
        self._depths = depths.astype(np.float64)


class Points(HoleData):
    """Class for storing and desurveying drill hole point data (i.e., data at specific depths along a drill hole). Inherits from `HoleData`."""

    def __init__(self):
        super().__init__()

        self.mesh = None
        self.surveys = None
        self.collars = None

    def add_data(
        self,
        hole_ids,
        depths,
        data,
        return_data=False,
        construct_categorical_cmap=False,
        **kwargs,
    ):
        """Add drill hole points data.

        Parameters
        ----------
        hole_ids : array-like
            Hole IDs as 1D array with length equal to the number of data points.

        depths : array-like
            Depths along drillhole path as 1D array with length equal to the number of data points.

        data : array-like or pandas DataFrame
            Matrix of drillhole data to be added. If a pandas DataFrame is passed, the column names will be used as the array names.

        return_data : bool, optional
            If True, returns the data.

        construct_categorical_cmap : bool, optional
            If True, constructs a categorical color map for the categorical array names upon displaying in Downhole plot or Plotter.

        """

        super().add_data(
            hole_ids,
            depths,
            data,
            return_data=return_data,
            construct_categorical_cmap=construct_categorical_cmap,
            **kwargs,
        )

    def desurvey(self, surveys):
        """Desurvey drill hole points data (i.e., determine the locations of each point in 3D space).

        Parameters
        ----------
        surveys : Surveys
            `drilldown.Surveys` object containing survey data for the drill holes.

        """

        if not isinstance(surveys, Surveys):
            raise TypeError("Surveys must be a Surveys object.")

        self.surveys = surveys
        self.collars = surveys.collars

        self.depths_desurveyed = np.empty((self.depths.shape[0], 3))

        for id in self.unique_hole_ids:
            hole_filter = self.hole_ids == id
            hole = self.surveys._holes[id]
            depths = self.depths[hole_filter]
            depths_desurveyed = hole.desurvey(depths)
            self.depths_desurveyed[hole_filter] = depths_desurveyed

    def make_mesh(self):
        """Make a mesh of the desurveyed drill hole points data.

        Returns
        -------
        pyvista.PolyData
            Mesh of the desurveyed drill hole points data.
        """
        meshes = None

        for id in self.unique_hole_ids:
            hole_filter = self.hole_ids == id
            depths = self.depths[hole_filter]

            if self.depths.shape[0] > 0:
                depths_desurveyed = self.depths_desurveyed[hole_filter]

                mesh = pv.PolyData(depths_desurveyed)

                for array_name in self.array_names_all:
                    data = self.data[array_name]["values"][hole_filter]
                    _type = self.data[array_name]["type"]
                    if _type == "str":
                        mesh[array_name] = data
                    else:
                        mesh.point_data[array_name] = data

                mesh.point_data["depth"] = depths
                mesh.point_data["hole ID"] = [
                    self.cat_to_code_map["hole ID"][id]
                ] * depths.shape[0]

                mesh.point_data["x"] = depths_desurveyed[:, 0]
                mesh.point_data["y"] = depths_desurveyed[:, 1]
                mesh.point_data["z"] = depths_desurveyed[:, 2]

                self.continuous_array_names += ["depth", "x", "y", "z"]

                if meshes is None:
                    meshes = mesh
                else:
                    meshes += mesh

        self.mesh = meshes

        return meshes

    def show(self, show_collars=False, show_surveys=False, *args, **kwargs):
        """Plot the desurveyed drill hole points data.
        Parameters
        ----------
        show_collars : bool, optional
            If True, also plots the associated collars using the corresponding `drilldown.Collars` object's mesh.

        show_surveys : bool, optional
            If True, also plots the associated surveys using the corresponding `drilldown.Surveys` object's mesh.

        """
        if self.mesh is None:
            self._construct_categorical_cmap()

        p = Plotter()
        p.matplotlib_formatted_color_maps = self.matplotlib_formatted_color_maps
        p.add_points(self, "points", selectable=False, *args, **kwargs)

        if show_collars == True:
            p.add_collars(self.collars)

        if show_surveys == True:
            p.add_surveys(self.surveys)

        return p.show()

    def drill_log(self, hole_id, log_array_names=[]):
        """Create a downhole plot containing points data for a given hole.

        Parameters
        ----------
        hole_id : str
            Hole ID corresponding to the hole for which the downhole plot is created.

        log_array_names : list of str, optional
            Names of the arrays to be plotted as subplots. If not provided, all arrays will be plotted.

        Returns
        -------
        plotly.graph_objects.Figure # TODO change to trame-based microapp

        """

        if hole_id not in self.unique_hole_ids:
            raise ValueError(f"Hole ID {hole_id} not present.")

        if self.construct_categorical_cmap == True:
            # ensure that color maps exist for categorical array names
            self._construct_categorical_cmap()

        log = DrillLog()

        depths = self.depths[self.hole_ids == hole_id]

        if isinstance(log_array_names, str):
            log_array_names = [log_array_names]
        if len(log_array_names) == 0:
            log_array_names = self.categorical_array_names + self.continuous_array_names

        for array_name in log_array_names:
            if array_name in self.categorical_array_names:
                values = self.data[array_name]["values"][self.hole_ids == hole_id]
                cat_to_color_map = self.cat_to_color_map.get(array_name, None)
                log.add_categorical_point_data(
                    array_name, depths, values, cat_to_color_map
                )

            elif array_name in self.continuous_array_names:
                values = self.data[array_name]["values"][self.hole_ids == hole_id]

                log.add_continuous_point_data(array_name, depths, values)

            else:
                raise ValueError(f"Data for array name {array_name} not present.")

        log.create_figure(y_axis_label="Depth (m)", title=hole_id)

        return log.fig


class Intervals(HoleData):
    """Class for storing and desurveying drill hole interval data (i.e., data between a from and to depth along a drill hole). Inherits from `HoleData`."""

    def __init__(self):
        super().__init__()

        self.mesh = None
        self.surveys = None
        self.collars = None

    def add_data(
        self,
        hole_ids,
        depths,
        data,
        return_data=False,
        construct_categorical_cmap=True,
        **kwargs,
    ):
        """Add drill hole interval data.

        Parameters
        ----------
        hole_ids : array-like
            Hole IDs as 1D array with length equal to the number of data points.

        depths : array-like
            Depths along drillhole path as 1D array with length equal to the number of data points.

        data : array-like or pandas DataFrame
            Matrix of drillhole data to be added. If a pandas DataFrame is passed, the column names will be used as the array names.

        return_data : bool, optional
            If True, returns the data.

        construct_categorical_cmap : bool, optional
            If True, constructs a categorical color map for the categorical array names upon displaying in Downhole plot or Plotter.

        Examples
        --------
        >>> import drilldown as dd
        >>> data_dict = dd.examples.load_tom_zone_macpass_project()
        >>> collar_data = data_dict["collar"]
        >>> survey_data = data_dict["survey"]
        >>> assay_data = data_dict["assay"]
        >>> collars = dd.Collars()
        >>> collars.add_data(collar_data["hole_ID"], collar_data[["x", "y", "z"]])
        >>> surveys = dd.Surveys()
        >>> surveys.add_data(survey_data["hole_ID"], survey_data["depth"], survey_data["azimuth"], survey_data["dip"])
        >>> surveys.locate(collars)
        >>> assays = dd.Intervals()
        >>> assays.add_data(assay_data["hole_ID"], assay_data[["from", "to"]], assay_data)
        >>> assays.desurvey(surveys)
        >>> assays.show()

        Returns
        -------
        dict, optional
            If `return_data` is True, returns the data.

        """
        super().add_data(
            hole_ids,
            depths,
            data,
            return_data=return_data,
            construct_categorical_cmap=construct_categorical_cmap,
            **kwargs,
        )

    def desurvey(self, surveys):
        """Desurvey drill hole interval data (i.e., determine the locations of each interval in 3D space).

        Parameters
        ----------
        surveys : Surveys
            `drilldown.Surveys` object containing survey data for the drill holes.

        """
        if not isinstance(surveys, Surveys):
            raise TypeError("Surveys must be a Surveys object.")

        self.surveys = surveys
        self.collars = surveys.collars

        self.from_depths_desurveyed = np.empty((self.depths.shape[0], 3))
        self.to_depths_desurveyed = np.empty((self.depths.shape[0], 3))
        self.intermediate_depths_desurveyed = np.empty((self.depths.shape[0], 3))

        for id in self.unique_hole_ids:
            hole_filter = self.hole_ids == id
            from_to = self.depths[hole_filter]
            hole = self.surveys._holes[id]

            from_depths_desurveyed = hole.desurvey(from_to[:, 0])
            to_depths_desurveyed = hole.desurvey(from_to[:, 1])
            intermediate_depths_desurveyed = np.mean(
                [from_depths_desurveyed, to_depths_desurveyed], axis=0
            )

            self.from_depths_desurveyed[hole_filter] = from_depths_desurveyed
            self.to_depths_desurveyed[hole_filter] = to_depths_desurveyed
            self.intermediate_depths_desurveyed[hole_filter] = (
                intermediate_depths_desurveyed
            )

    def make_mesh(self):
        """
        Make a mesh of the desurveyed drill hole interval data.

        Returns
        -------
        pyvista.PolyData
            Mesh of the desurveyed drill hole interval data.

        """
        meshes = None

        for id in self.unique_hole_ids:
            hole = self.surveys._holes[id]
            hole_filter = self.hole_ids == id
            from_to = self.depths[hole_filter]

            if from_to.shape[0] > 0:
                from_depths_desurveyed = self.from_depths_desurveyed[hole_filter]
                to_depths_desurveyed = self.to_depths_desurveyed[hole_filter]
                intermediate_depths_desurveyed = self.intermediate_depths_desurveyed[
                    hole_filter
                ]

                mesh = hole._make_line_mesh(
                    from_depths_desurveyed, to_depths_desurveyed
                )

                mesh.cell_data["from"] = from_to[:, 0]
                mesh.cell_data["to"] = from_to[:, 1]
                mesh.cell_data["hole ID"] = [
                    self.cat_to_code_map["hole ID"][id]
                ] * from_to.shape[0]

                mesh.cell_data["x"] = intermediate_depths_desurveyed[:, 0]
                mesh.cell_data["y"] = intermediate_depths_desurveyed[:, 1]
                mesh.cell_data["z"] = intermediate_depths_desurveyed[:, 2]

                self.continuous_array_names += ["from", "to", "x", "y", "z"]

                for array_name in self.array_names_all:
                    data = self.data[array_name]["values"][hole_filter]
                    _type = self.data[array_name]["type"]
                    if _type == "str":
                        mesh[array_name] = data
                    else:
                        mesh.cell_data[array_name] = data
                if meshes is None:
                    meshes = mesh
                else:
                    meshes += mesh

        self.mesh = meshes

        return meshes

    def show(self, show_collars=False, show_surveys=False, *args, **kwargs):
        """Plot the desurveyed drill hole interval data.

        Parameters
        ----------
        show_collars : bool, optional
            If True, also plots the associated collars using the corresponding `drilldown.Collars` object's mesh.

        show_surveys : bool, optional
            If True, also plots the associated surveys using the corresponding `drilldown.Surveys` object's mesh.

        """
        if self.mesh is None:
            self._construct_categorical_cmap()

        p = Plotter()
        p.add_intervals(self, "intervals", selectable=False, *args, **kwargs)

        if show_collars == True:
            p.add_collars(self.collars)

        if show_surveys == True:
            p.add_surveys(self.surveys)

        return p.show()

    def drill_log(self, hole_id, log_array_names=[]):
        """Create a downhole plot displaying interval data for a given hole.

        Parameters
        ----------
        hole_id : str
            Hole ID corresponding to the hole for which the downhole plot is created.

        log_array_names : list of str, optional
            Names of the arrays to be plotted as subplots. If not provided, all arrays will be plotted.

        Examples
        --------
        Display a downhole plot containing assay data for a given hole.

        >>> import drilldown as dd
        >>> data_dict = dd.examples.load_tom_zone_macpass_project()
        >>> collar_data = data_dict["collar"]
        >>> survey_data = data_dict["survey"]
        >>> assay_data = data_dict["assay"]
        >>> collars = dd.Collars()
        >>> collars.add_data(collar_data["hole_ID"], collar_data[["x", "y", "z"]])
        >>> surveys = dd.Surveys()
        >>> surveys.add_data(survey_data["hole_ID"], survey_data["depth"], survey_data["azimuth"], survey_data["dip"])
        >>> surveys.locate(collars)
        >>> assays = dd.Intervals()
        >>> assays.add_data(assay_data["hole_ID"], assay_data[["from", "to"]], assay_data)
        >>> assays.desurvey(surveys)
        >>> assays.drill_log("TU001", log_array_names=["Pb_pct", "Zn_pct"])

        Returns
        -------
        plotly.graph_objects.Figure # TODO change to trame-based microapp

        """
        if hole_id not in self.unique_hole_ids:
            raise ValueError(f"Hole ID {hole_id} not present.")

        if self.construct_categorical_cmap == True:
            # ensure that color maps exist for categorical array names
            self._construct_categorical_cmap()

        log = DrillLog()

        from_to = self.depths[self.hole_ids == hole_id]

        if isinstance(log_array_names, str):
            log_array_names = [log_array_names]
        if len(log_array_names) == 0:
            log_array_names = self.categorical_array_names + self.continuous_array_names

        for array_name in log_array_names:
            if array_name in self.categorical_array_names:
                values = self.data[array_name]["values"][self.hole_ids == hole_id]
                values = np.array(
                    [self.code_to_cat_map[array_name][val] for val in values]
                )
                cat_to_color_map = self.cat_to_color_map.get(array_name, None)
                log.add_categorical_interval_data(
                    array_name, from_to, values, cat_to_color_map
                )

            elif array_name in self.continuous_array_names:
                values = self.data[array_name]["values"][self.hole_ids == hole_id]

                log.add_continuous_interval_data(array_name, from_to, values)

            else:
                raise ValueError(f"Data for array name {array_name} not present.")

        log.create_figure(y_axis_label="Depth (m)", title=hole_id)

        return log.fig


class Collars:
    """Class for storing drill hole collar data."""

    def __init__(self):
        self.unique_hole_ids = None
        self.coords = None
        self.mesh = None

    def add_data(self, hole_ids, coords):
        """Add drill hole collar data.

        Parameters
        ----------
        hole_ids : array-like
            Hole IDs as 1D array.

        coords : array-like
            Coordinates of the drill hole collars as 2D array with shape (n, 3), where n is the number of collars.

        Examples
        --------
        >>> import drilldown as dd
        >>> data_dict = dd.examples.load_tom_zone_macpass_project()
        >>> collar_data = data_dict["collar"]
        >>> collars = dd.Collars()
        >>> collars.add_data(collar_data["hole_ID"], collar_data[["x", "y", "z"]])

        """
        hole_ids = convert_to_numpy_array(hole_ids)
        coords = convert_to_numpy_array(coords)

        if coords.ndim != 2:
            raise ValueError("Coordinates must be 2-dimensional.")

        if coords.shape[1] != 3:
            raise ValueError("Coordinates must have 3 columns.")

        if hole_ids.shape[0] != coords.shape[0]:
            raise ValueError("Hole IDs and coordinates must have the same length.")

        self.unique_hole_ids = np.unique(hole_ids)
        self.coords = np.c_[hole_ids, coords]

    def make_mesh(self):
        """Make a mesh of the drill hole collar data.

        Returns
        -------
        pyvista.PolyData
            Mesh of the drill hole collar data.

        """
        mesh = pv.PolyData(np.asarray(self.coords[:, 1:], dtype="float"))
        mesh["hole ID"] = self.coords[:, 0]
        self.mesh = mesh
        return mesh

    def show(self, *args, **kwargs):
        """Plot the drill hole collar data."""
        p = Plotter()
        p.add_collars(self, *args, **kwargs)

        return p.show()


class Surveys:
    """Class for storing drill hole survey data."""

    def __init__(self):
        self.unique_hole_ids = None
        self.measurements = None
        self.collars = None
        self._holes = {}
        self.mesh = None

    def add_data(self, hole_ids, dist, azm, dip):
        """Add drill hole survey data.

        Parameters
        ----------
        hole_ids : array-like
            Hole IDs as 1D array with length equal to the number of survey measurements.

        dist : array-like
            Distance along drillhole path for each survey measurement, as 1D array with length equal to the number of survey measurements.

        azm : array-like
            Azimuth for each survey measurement (i.e., orientation angle in horizontal plane), as 1D array with length equal to the number of survey measurements.

        dip : array-like
            Dip for each survey measurement (i.e., orientation angle in vertical plane), as 1D array with length equal to the number of survey measurements.

        Examples
        --------
        >>> import drilldown as dd
        >>> data_dict = dd.examples.load_tom_zone_macpass_project()
        >>> survey_data = data_dict["survey"]
        >>> surveys = dd.Surveys()
        >>> surveys.add_data(survey_data["hole_ID"], survey_data["depth"], survey_data["azimuth"], survey_data["dip"])

        """
        hole_ids = convert_to_numpy_array(hole_ids)
        dist = convert_to_numpy_array(dist)
        azm = convert_to_numpy_array(azm)
        dip = convert_to_numpy_array(dip)

        if (dist.ndim != 1) | (azm.ndim != 1) | (dip.ndim != 1):
            raise ValueError("Survey measurements must be 1-dimensional.")

        if not hole_ids.shape[0] == dist.shape[0] == azm.shape[0] == dip.shape[0]:
            raise ValueError(
                "Hole IDs and survey measurements must have the same length."
            )

        if np.any(np.abs(dip) > 90):
            raise ValueError("Dip values must be between -90 and 90 degrees.")

        self.unique_hole_ids = np.unique(hole_ids)
        self.measurements = np.c_[hole_ids, dist, azm, dip]

        if self.collars is not None:
            self._create_holes()

    def locate(self, collars):
        """Locate the drill hole survey data (i.e., position the top of each hole in 3D space) using the drill hole collar data.

        Parameters
        ----------
        collars : Collars
            `drilldown.Collars` object containing the drill hole collar data.

        """
        self.collars = collars
        for hole_id in self.unique_hole_ids:
            hole = DrillHole()

            hole.add_collar(collars.coords[collars.coords[:, 0] == hole_id, 1:][0])

            measurements = np.hsplit(
                self.measurements[self.measurements[:, 0] == hole_id, 1:], 3
            )
            if (measurements[0].shape[0]) > 0:
                hole.add_survey(measurements[0], measurements[1], measurements[2])

                hole._create_hole()

                self._holes[hole_id] = hole

    def make_mesh(self):
        """Make a mesh of the drill hole survey data.

        Returns
        -------
        pyvista.PolyData
            Mesh of the drill hole survey data.

        """
        mesh = None
        for hole_id in self._holes.keys():
            hole = self._holes[hole_id]
            depths = hole.desurvey()
            from_to = hole._make_from_to(depths)
            if from_to.shape[0] > 0:
                if mesh is None:
                    mesh = hole._make_line_mesh(from_to[:, 0], from_to[:, 1])
                else:
                    mesh += hole._make_line_mesh(from_to[:, 0], from_to[:, 1])

        self.mesh = mesh

        return mesh

    def show(self, show_collars=False, *args, **kwargs):
        """Plot the drill hole survey data.

        Parameters
        ----------
        show_collars : bool, optional
            If True, also plots the associated collars using the corresponding `drilldown.Collars` object's mesh.

        """
        p = Plotter()
        p.add_surveys(self, *args, **kwargs)

        if show_collars == True:
            p.add_collars(self.collars)

        return p.show()


class DrillHole:
    def __init__(self):
        self.workspace = Workspace()
        self.hole_group = DrillholeGroup.create(self.workspace)
        self.array_names = []
        self.categorical_interval_array_names = []
        self.continuous_interval_array_names = []
        self.categorical_point_array_names = []
        self.continuous_point_array_names = []

        self.intervals = {}
        self.points = {}

        self.cat_to_code_map = {}
        self.code_to_cat_map = {}
        self.cat_to_color_map = {}
        self.matplotlib_formatted_color_maps = {}

    def add_collar(self, collar):
        if isinstance(collar, pd.core.series.Series) | isinstance(
            collar, pd.core.frame.DataFrame
        ):
            collar = collar.values[0]

        self.collar = collar

    def add_survey(self, dist, azm, dip):
        if isinstance(dist, pd.core.series.Series):
            dist = dist.values
        if isinstance(azm, pd.core.series.Series):
            azm = azm.values
        if isinstance(dip, pd.core.series.Series):
            dip = dip.values

        self.survey = np.c_[dist, azm, dip]
        self._create_hole()

    def _create_hole(self):
        self._hole = Drillhole.create(
            self.workspace,
            collar=self.collar,
            surveys=self.survey,
            name="",
            parent=self.hole_group,
        )

    def _add_from_to(self, from_to):
        if isinstance(from_to, pd.core.frame.DataFrame):
            from_to = from_to.values
        self.from_to = from_to.astype(np.float64)

        return self.from_to

    def _make_from_to(self, depths, connected=True):
        if connected == True:
            from_depths = depths[:-1]
            to_depths = depths[1:]

            if depths.ndim == 1:
                depths = np.empty([from_depths.shape[0], 2])
            else:
                depths = np.empty([from_depths.shape[0], 2, from_depths.shape[1]])

            depths[:, 0] = from_depths
            depths[:, 1] = to_depths

            return depths

    def _add_data(self, data, name=None):
        self.cat_to_code_map[name] = data.cat_to_code_map
        self.code_to_cat_map[name] = data.code_to_cat_map
        self.cat_to_color_map[name] = data.cat_to_color_map
        self.matplotlib_formatted_color_maps[name] = (
            data.matplotlib_formatted_color_maps
        )

    def add_intervals(self, intervals, name="intervals"):
        self.intervals[name] = intervals
        self.categorical_interval_array_names += intervals.categorical_array_names
        self.continuous_interval_array_names += intervals.continuous_array_names
        self.array_names += intervals.array_names_all
        self._add_data(intervals, name)

    def add_points(self, points, name="points"):
        self.points[name] = points
        self.categorical_point_array_names += points.categorical_array_names
        self.continuous_point_array_names += points.continuous_array_names
        self.array_names += points.array_names_all
        self._add_data(points, name)

    def desurvey(self, depths=None):
        if depths is None:
            # return desurveyed survey depths if no depths passed
            return self._hole.desurvey(self.survey[:, 0])
        else:
            return self._hole.desurvey(depths)

    def _make_line_mesh(self, from_depth, to_depth):
        """Make a mesh consisting of line segments for which a connected topology is assumed."""
        depths = np.empty((from_depth.shape[0] + to_depth.shape[0], 3))
        depths[0::2, :] = from_depth
        depths[1::2, :] = to_depth
        n_connected = np.ones(int(depths.shape[0] / 2), dtype="int") * 2
        from_positions = np.arange(0, depths.shape[0] - 1, 2)
        to_positions = np.arange(1, depths.shape[0], 2)
        depth_connectivity = np.hstack(
            np.stack([n_connected, from_positions, to_positions], axis=1)
        )
        mesh = pv.PolyData(depths, lines=depth_connectivity)

        return mesh

    def make_collar_mesh(self):
        mesh = pv.PolyData(self.collar)
        mesh["hole ID"] = self._hole.name

        return mesh

    def make_survey_mesh(self):
        depths = self.desurvey()
        from_to = self._make_from_to(depths)
        mesh = self._make_line_mesh(from_to[0], from_to[1])

        return mesh

    def make_intervals_mesh(self, name=None):
        if name is None:
            name = list(self.intervals.keys())[0]

        intervals = self.intervals[name]
        from_to = intervals.depths
        from_depths = self.desurvey(from_to[:, 0])
        to_depths = self.desurvey(from_to[:, 1])
        intermediate_depths = np.mean([from_depths, to_depths], axis=0)
        mesh = self._make_line_mesh(from_depths, to_depths)

        mesh.cell_data["from"] = from_to[:, 0]
        mesh.cell_data["to"] = from_to[:, 1]
        mesh.cell_data["x"] = intermediate_depths[:, 0]
        mesh.cell_data["y"] = intermediate_depths[:, 1]
        mesh.cell_data["z"] = intermediate_depths[:, 2]
        self.continuous_interval_array_names += ["from", "to", "x", "y", "z"]
        for array_name in intervals.array_names_all:
            data = intervals.data[array_name]["values"]
            _type = intervals.data[array_name]["type"]
            if _type == "str":
                mesh[array_name] = data
            else:
                mesh.cell_data[array_name] = data

        return mesh

    def make_points_mesh(self, name=None):
        if name is None:
            name = list(self.points.keys())[0]

        points = self.points[name]
        depths = points.depths
        depths = self.desurvey(depths)
        mesh = pv.PolyData(depths)
        for array_name in points.array_names_all:
            data = points.data[array_name]["values"]
            _type = points.data[array_name]["type"]
            if _type == "str":
                mesh[array_name] = data
            else:
                mesh.point_data[array_name] = data

        return mesh

    def show_collar(self, *args, **kwargs):
        collar_mesh = self.make_collar_mesh()
        p = Plotter()
        p.add_collars(collar_mesh, *args, **kwargs)

        return p.show()

    def show_survey(self, show_collar=False, *args, **kwargs):
        survey_mesh = self.make_survey_mesh()
        p = Plotter()
        p.add_surveys(survey_mesh, *args, **kwargs)

        if show_collar == True:
            collar_mesh = self.make_collar_mesh()
            p.add_collars(collar_mesh)

        return p.show()

    def show_intervals(
        self, name=None, show_collar=False, show_survey=False, *args, **kwargs
    ):
        if name is None:
            name = list(self.intervals.keys())[0]

        intervals_mesh = self.make_intervals_mesh(name)
        p = Plotter()
        p.add_intervals(
            intervals_mesh,
            name,
            self.categorical_interval_array_names,
            self.continuous_interval_array_names,
            *args,
            **kwargs,
        )

        if show_collar == True:
            collar_mesh = self.make_collar_mesh()
            p.add_collars(collar_mesh)

        if show_survey == True:
            survey_mesh = self.make_survey_mesh()
            p.add_surveys(survey_mesh)

        return p.show()

    def show_points(
        self, name=None, show_collar=False, show_survey=False, *args, **kwargs
    ):
        if name is None:
            name = list(self.points.keys())[0]

        points_mesh = self.make_points_mesh(name)
        p = Plotter()
        p.add_points(
            points_mesh,
            name,
            self.categorical_point_array_names,
            self.continuous_point_array_names,
            *args,
            **kwargs,
        )

        if show_collar == True:
            collar_mesh = self.make_collar_mesh()
            p.add_collars(collar_mesh)

        if show_survey == True:
            survey_mesh = self.make_survey_mesh()
            p.add_surveys(survey_mesh)

        return p.show()

    def show(self):
        collar_mesh = self.make_collar_mesh()
        survey_mesh = self.make_survey_mesh()
        intervals_mesh = self.make_intervals_mesh()
        points_mesh = self.make_points_mesh()

        p = Plotter()
        p.add_collars(collar_mesh)
        p.add_surveys(survey_mesh)

        intervals_name = list(self.intervals.keys())[0]
        p.add_intervals(
            intervals_mesh,
            intervals_name,
            self.categorical_interval_array_names,
            self.continuous_interval_array_names,
            radius=10,
        )

        points_name = list(self.points.keys())[0]
        p.add_points(
            points_mesh,
            points_name,
            self.categorical_point_array_names,
            self.continuous_point_array_names,
        )

        return p.show()

    def drill_log(self, log_array_names=[]):
        if self.intervals or self.points:  # ensure there is data to plot
            log = DrillLog()
            if len(log_array_names) == 0:
                interval_array_names = (
                    self.categorical_interval_array_names
                    + self.continuous_interval_array_names
                )
                point_array_names = (
                    self.categorical_point_array_names
                    + self.continuous_point_array_names
                )
                log_array_names = interval_array_names + point_array_names

            for array_name in log_array_names:
                for name in self.intervals.keys():
                    intervals = self.intervals[name]
                    from_to = intervals.depths
                    if array_name in intervals.categorical_array_names:
                        cat_to_color_map = self.cat_to_color_map[name]
                        values = intervals.data[array_name]["values"]
                        values = np.array(
                            [
                                self.code_to_cat_map[name][array_name][val]
                                for val in values
                            ]
                        )
                        log.add_categorical_interval_data(
                            array_name,
                            from_to,
                            values,
                            cat_to_color_map.get(array_name, None),
                        )

                        exit_flag = True
                        break

                    elif array_name in intervals.continuous_array_names:
                        values = intervals.data[array_name]["values"]

                        log.add_continuous_interval_data(array_name, from_to, values)

                        exit_flag = True
                        break

                    if exit_flag == True:
                        break

                for name in self.points.keys():
                    points = self.points[name]
                    depths = points.depths
                    if array_name in points.categorical_array_names:
                        cat_to_color_map = self.cat_to_color_map[name]
                        values = points.data[array_name]["values"]
                        values = np.array(
                            [
                                self.code_to_cat_map[name][array_name][val]
                                for val in values
                            ]
                        )
                        log.add_categorical_point_data(
                            array_name,
                            depths,
                            values,
                            cat_to_color_map.get(array_name, None),
                        )

                        exit_flag = True
                        break

                    elif array_name in self.continuous_point_array_names:
                        pass

                    if exit_flag == True:
                        break

            log.create_figure(y_axis_label="Depth (m)")

            return log.fig
