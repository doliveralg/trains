import collections
import json

import cv2
import six

from ..base import InterfaceBase
from ..setupuploadmixin import SetupUploadMixin
from ...utilities.async_manager import AsyncManagerMixin
from ...utilities.plotly_reporter import create_2d_histogram_plot, create_value_matrix, create_3d_surface, \
    create_2d_scatter_series, create_3d_scatter_series, create_line_plot, plotly_scatter3d_layout_dict, \
    create_image_plot
from ...utilities.py3_interop import AbstractContextManager
from .events import ScalarEvent, VectorEvent, ImageEvent, PlotEvent, ImageEventNoUpload


class Reporter(InterfaceBase, AbstractContextManager, SetupUploadMixin, AsyncManagerMixin):
    """
    A simple metrics reporter class.
    This class caches reports and supports both a explicit flushing and context-based flushing. To ensure reports are
     sent to the backend, please use (assuming an instance of Reporter named 'reporter'):
     - use the context manager feature (which will automatically flush when exiting the context):
        with reporter:
            reporter.report...
            ...
     - explicitly call flush:
        reporter.report...
        ...
        reporter.flush()
    """

    def __init__(self, metrics, flush_threshold=10, async_enable=False):
        """
        Create a reporter
        :param metrics: A Metrics manager instance that handles actual reporting, uploads etc.
        :type metrics: .backend_interface.metrics.Metrics
        :param flush_threshold: Events flush threshold. This determines the threshold over which cached reported events
            are flushed and sent to the backend.
        :type flush_threshold: int
        """
        log = metrics.log.getChild('reporter')
        log.setLevel(log.level)
        super(Reporter, self).__init__(session=metrics.session, log=log)
        self._metrics = metrics
        self._flush_threshold = flush_threshold
        self._events = []
        self._bucket_config = None
        self._storage_uri = None
        self._async_enable = async_enable

    def _set_storage_uri(self, value):
        value = '/'.join(x for x in (value.rstrip('/'), self._metrics.storage_key_prefix) if x)
        self._storage_uri = value

    storage_uri = property(None, _set_storage_uri)

    @property
    def flush_threshold(self):
        return self._flush_threshold

    @flush_threshold.setter
    def flush_threshold(self, value):
        self._flush_threshold = max(0, value)

    @property
    def async_enable(self):
        return self._async_enable

    @async_enable.setter
    def async_enable(self, value):
        self._async_enable = bool(value)

    def _report(self, ev):
        self._events.append(ev)
        if len(self._events) >= self._flush_threshold:
            self._write()

    def _write(self):
        if not self._events:
            return
        # print('reporting %d events' % len(self._events))
        res = self._metrics.write_events(self._events, async_enable=self._async_enable, storage_uri=self._storage_uri)
        if self._async_enable:
            self._add_async_result(res)
        self._events = []

    def flush(self):
        """
        Flush cached reports to backend.
        """
        self._write()
        # wait for all reports
        if self.get_num_results() > 0:
            self.wait_for_results()

    def report_scalar(self, title, series, value, iter):
        """
        Report a scalar value
        :param title: Title (AKA metric)
        :type title: str
        :param series: Series (AKA variant)
        :type series: str
        :param value: Reported value
        :type value: float
        :param iter: Iteration number
        :type value: int
        """
        ev = ScalarEvent(metric=self._normalize_name(title),
                         variant=self._normalize_name(series), value=value, iter=iter)
        self._report(ev)

    def report_vector(self, title, series, values, iter):
        """
        Report a vector of values
        :param title: Title (AKA metric)
        :type title: str
        :param series: Series (AKA variant)
        :type series: str
        :param values: Reported values
        :type value: [float]
        :param iter: Iteration number
        :type value: int
        """
        if not isinstance(values, collections.Iterable):
            raise ValueError('values: expected an iterable')
        ev = VectorEvent(metric=self._normalize_name(title),
                         variant=self._normalize_name(series), values=values, iter=iter)
        self._report(ev)

    def report_plot(self, title, series, plot, iter):
        """
        Report a Plotly chart
        :param title: Title (AKA metric)
        :type title: str
        :param series: Series (AKA variant)
        :type series: str
        :param plot: A JSON describing a plotly chart (see https://help.plot.ly/json-chart-schema/)
        :type plot: str or dict
        :param iter: Iteration number
        :type value: int
        """
        if isinstance(plot, dict):
            plot = json.dumps(plot)
        elif not isinstance(plot, six.string_types):
            raise ValueError('Plot should be a string or a dict')
        ev = PlotEvent(metric=self._normalize_name(title),
                       variant=self._normalize_name(series), plot_str=plot, iter=iter)
        self._report(ev)

    def report_image(self, title, series, src, iter):
        """
        Report an image.
        :param title: Title (AKA metric)
        :type title: str
        :param series: Series (AKA variant)
        :type series: str
        :param src: Image source URI. This URI will be used by the webapp and workers when trying to obtain the image
            for presentation of processing. Currently only http(s), file and s3 schemes are supported.
        :type src: str
        :param iter: Iteration number
        :type value: int
        """
        ev = ImageEventNoUpload(metric=self._normalize_name(title),
                                variant=self._normalize_name(series), iter=iter, src=src)
        self._report(ev)

    def report_image_and_upload(self, title, series, iter, path=None, matrix=None, upload_uri=None,
                                max_image_history=None):
        """
        Report an image and upload its contents. Image is uploaded to a preconfigured bucket (see setup_upload()) with
         a key (filename) describing the task ID, title, series and iteration.
        :param title: Title (AKA metric)
        :type title: str
        :param series: Series (AKA variant)
        :type series: str
        :param iter: Iteration number
        :type value: int
        :param path: A path to an image file. Required unless matrix is provided.
        :type path: str
        :param matrix: A 3D numpy.ndarray object containing image data (BGR). Required unless filename is provided.
        :type matrix: str
        :param max_image_history: maximum number of image to store per metric/variant combination
        use negative value for unlimited. default is set in global configuration (default=5)
        """
        if not self._storage_uri and not upload_uri:
            raise ValueError('Upload configuration is required (use setup_upload())')
        if len([x for x in (path, matrix) if x is not None]) != 1:
            raise ValueError('Expected only one of [filename, matrix]')
        kwargs = dict(metric=self._normalize_name(title),
                      variant=self._normalize_name(series), iter=iter, image_file_history_size=max_image_history)
        ev = ImageEvent(image_data=matrix, upload_uri=upload_uri, local_image_path=path, **kwargs)
        self._report(ev)

    def report_histogram(self, title, series, histogram, iter, labels=None, xlabels=None, comment=None):
        """
        Report an histogram bar plot
        :param title: Title (AKA metric)
        :type title: str
        :param series: Series (AKA variant)
        :type series: str
        :param histogram: The histogram data.
            A row for each dataset(bar in a bar group). A column for each bucket.
        :type histogram: numpy array
        :param iter: Iteration number
        :type value: int
        :param labels: The labels for each bar group.
        :type labels: list of strings.
        :param xlabels: The labels of the x axis.
        :type xlabels: List of strings.
        :param comment: comment underneath the title
        :type comment: str
        """
        plotly_dict = create_2d_histogram_plot(
            np_row_wise=histogram,
            title=title,
            labels=labels,
            series=series,
            xlabels=xlabels,
            comment=comment,
        )

        return self.report_plot(
            title=self._normalize_name(title),
            series=self._normalize_name(series),
            plot=plotly_dict,
            iter=iter,
        )

    def report_line_plot(self, title, series, iter, xtitle, ytitle, mode='lines', reverse_xaxis=False, comment=None):
        """
        Report a (possibly multiple) line plot.

        :param title: Title (AKA metric)
        :type title: str
        :param series: All the series' data, one for each line in the plot.
        :type series: An iterable of LineSeriesInfo.
        :param iter: Iteration number
        :type iter: int
        :param xtitle: x-axis title
        :type xtitle: str
        :param ytitle: y-axis title
        :type ytitle: str
        :param mode: 'lines' / 'markers' / 'lines+markers'
        :type mode: str
        :param reverse_xaxis: If true X axis will be displayed from high to low (reversed)
        :type reverse_xaxis: bool
        :param comment: comment underneath the title
        :type comment: str
        """

        plotly_dict = create_line_plot(
            title=title,
            series=series,
            xtitle=xtitle,
            ytitle=ytitle,
            mode=mode,
            reverse_xaxis=reverse_xaxis,
            comment=comment,
        )

        return self.report_plot(
            title=self._normalize_name(title),
            series='',
            plot=plotly_dict,
            iter=iter,
        )

    def report_2d_scatter(self, title, series, data, iter, mode='lines', xtitle=None, ytitle=None, labels=None,
                          comment=None):
        """
        Report a 2d scatter graph (with lines)

        :param title: Title (AKA metric)
        :type title: str
        :param series: Series (AKA variant)
        :type series: str
        :param data: A scattered data: pairs of x,y as rows in a numpy array
        :type scatter: ndarray
        :param iter: Iteration number
        :type iter: int
        :param mode: (type str) 'lines'/'markers'/'lines+markers'
        :param xtitle: optional x-axis title
        :param ytitle: optional y-axis title
        :param labels: label (text) per point in the scatter (in the same order)
        :param comment: comment underneath the title
        :type comment: str
        """
        plotly_dict = create_2d_scatter_series(
            np_row_wise=data,
            title=title,
            series_name=series,
            mode=mode,
            xtitle=xtitle,
            ytitle=ytitle,
            labels=labels,
            comment=comment,
        )

        return self.report_plot(
            title=self._normalize_name(title),
            series=self._normalize_name(series),
            plot=plotly_dict,
            iter=iter,
        )

    def report_3d_scatter(self, title, series, data, iter, labels=None, mode='lines', color=((217, 217, 217, 0.14),),
                          marker_size=5, line_width=0.8, xtitle=None, ytitle=None, ztitle=None, fill=None,
                          comment=None):
        """
        Report a 3d scatter graph (with markers)

        :param title: Title (AKA metric)
        :type title: str
        :param series: Series (AKA variant)
        :type series: str
        :param data: A scattered data: pairs of x,y,z as rows in a numpy array. or list of numpy arrays
        :type data: ndarray.
        :param iter: Iteration number
        :type iter: int
        :param labels: label (text) per point in the scatter (in the same order)
        :type labels: str
        :param mode: (type str) 'lines'/'markers'/'lines+markers'
        :param color: list of RGBA colors [(217, 217, 217, 0.14),]
        :param marker_size: marker size in px
        :param line_width: line width in px
        :param xtitle: optional x-axis title
        :param ytitle: optional y-axis title
        :param ztitle: optional z-axis title
        :param comment: comment underneath the title
        """
        data_series = data if isinstance(data, list) else [data]

        def get_labels(i):
            if labels and isinstance(labels, list):
                try:
                    item = labels[i]
                except IndexError:
                    item = labels[-1]
                if isinstance(item, list):
                    return item
            return labels

        plotly_obj = plotly_scatter3d_layout_dict(
            title=title,
            xaxis_title=xtitle,
            yaxis_title=ytitle,
            zaxis_title=ztitle,
            comment=comment,
        )

        for i, values in enumerate(data_series):
            plotly_obj = create_3d_scatter_series(
                np_row_wise=values,
                title=title,
                series_name=series[i] if isinstance(series, list) else None,
                labels=get_labels(i),
                plotly_obj=plotly_obj,
                mode=mode,
                line_width=line_width,
                marker_size=marker_size,
                color=color,
                fill_axis=fill,
            )

        return self.report_plot(
            title=self._normalize_name(title),
            series=self._normalize_name(series) if not isinstance(series, list) else None,
            plot=plotly_obj,
            iter=iter,
        )

    def report_value_matrix(self, title, series, data, iter, xlabels=None, ylabels=None, comment=None):
        """
        Report a heat-map matrix

        :param title: Title (AKA metric)
        :type title: str
        :param series: Series (AKA variant)
        :type series: str
        :param data: A heat-map matrix (example: confusion matrix)
        :type data: ndarray
        :param iter: Iteration number
        :type iter: int
        :param xlabels: optional label per column of the matrix
        :param ylabels: optional label per row of the matrix
        :param comment: comment underneath the title
        """

        plotly_dict = create_value_matrix(
            np_value_matrix=data,
            title=title,
            xlabels=xlabels,
            ylabels=ylabels,
            series=series,
            comment=comment,
        )

        return self.report_plot(
            title=self._normalize_name(title),
            series=self._normalize_name(series),
            plot=plotly_dict,
            iter=iter,
        )

    def report_value_surface(self, title, series, data, iter, xlabels=None, ylabels=None,
                             xtitle=None, ytitle=None, ztitle=None, camera=None, comment=None):
        """
        Report a 3d surface (same data as heat-map matrix, only presented differently)

        :param title: Title (AKA metric)
        :type title: str
        :param series: Series (AKA variant)
        :type series: str
        :param data: A heat-map matrix (example: confusion matrix)
        :type data: ndarray
        :param iter: Iteration number
        :type iter: int
        :param xlabels: optional label per column of the matrix
        :param ylabels: optional label per row of the matrix
        :param xtitle: optional x-axis title
        :param ytitle: optional y-axis title
        :param ztitle: optional z-axis title
        :param camera: X,Y,Z camera position. def: (1,1,1)
        :param comment: comment underneath the title
        """

        plotly_dict = create_3d_surface(
            np_value_matrix=data,
            title=title + '/' + series,
            xlabels=xlabels,
            ylabels=ylabels,
            series=series,
            xtitle=xtitle,
            ytitle=ytitle,
            ztitle=ztitle,
            camera=camera,
            comment=comment,
        )

        return self.report_plot(
            title=self._normalize_name(title),
            series=self._normalize_name(series),
            plot=plotly_dict,
            iter=iter,
        )

    def report_image_plot_and_upload(self, title, series, iter, path=None, matrix=None,
                                     upload_uri=None, max_image_history=None):
        """
        Report an image as plot and upload its contents.
        Image is uploaded to a preconfigured bucket (see setup_upload()) with a key (filename)
        describing the task ID, title, series and iteration.
        Then a plotly object is created and registered, this plotly objects points to the uploaded image
        :param title: Title (AKA metric)
        :type title: str
        :param series: Series (AKA variant)
        :type series: str
        :param iter: Iteration number
        :type value: int
        :param path: A path to an image file. Required unless matrix is provided.
        :type path: str
        :param matrix: A 3D numpy.ndarray object containing image data (BGR). Required unless filename is provided.
        :type matrix: str
        :param max_image_history: maximum number of image to store per metric/variant combination
        use negative value for unlimited. default is set in global configuration (default=5)
        """
        if not upload_uri and not self._storage_uri:
            raise ValueError('Upload configuration is required (use setup_upload())')
        if len([x for x in (path, matrix) if x is not None]) != 1:
            raise ValueError('Expected only one of [filename, matrix]')
        kwargs = dict(metric=self._normalize_name(title),
                      variant=self._normalize_name(series), iter=iter, image_file_history_size=max_image_history)
        ev = ImageEvent(image_data=matrix, upload_uri=upload_uri, local_image_path=path, **kwargs)
        _, url = ev.get_target_full_upload_uri(upload_uri or self._storage_uri, self._metrics.storage_key_prefix)
        self._report(ev)
        plotly_dict = create_image_plot(
            image_src=url,
            title=title + '/' + series,
            width=matrix.shape[1] if matrix is not None else 640,
            height=matrix.shape[0] if matrix is not None else 480,
        )

        return self.report_plot(
            title=self._normalize_name(title),
            series=self._normalize_name(series),
            plot=plotly_dict,
            iter=iter,
        )

    @classmethod
    def _normalize_name(cls, name):
        if not name:
            return name
        return name.replace('$', '/').replace('.', '/')

    def __exit__(self, exc_type, exc_val, exc_tb):
        # don't flush in case an exception was raised
        if not exc_type:
            self.flush()
