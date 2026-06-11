# -*- coding: utf-8 -*-
def classFactory(iface):
    from .sits_timeseries import SITSTimeSeriesPlugin
    return SITSTimeSeriesPlugin(iface)
