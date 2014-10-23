"""
Auto-download of ancillary files.

It allows Operations to specify a serious of source locations (http/ftp/rss URLs)
and destination locations to download to.

This is intended to replace Operations maintenance of many diverse and
complicated scripts with a single, central configuration file.
"""
import logging
import sys

from . import http, ftp, DataSource, FetchReporter, RegexpOutputPathTransform


_log = logging.getLogger(__name__)


class _PrintReporter(FetchReporter):
    """
    Send events to the log.
    """

    def file_complete(self, uri, name, path):
        """
        :type uri: str
        :type name: str
        :type path: str
        """
        _log.info('Completed %r: %r -> %r', name, uri, path)

    def file_error(self, uri, message):
        """
        :type uri: str
        :type message: str
        """
        _log.info('Error (%r): %r)', uri, message)


def execute_modules(modules):
    """
    Execute the given modules once.

    :type modules: list of DataSource
    :return:
    """
    reporter = _PrintReporter()
    # TODO: Filter based on module period (daily, hourly etc).
    # TODO: Parallel execution
    for module in modules:
        _log.info('Running %s: %r', DataSource.__name__, module)
        try:
            module.trigger(reporter)
        except KeyboardInterrupt:
            raise
        except:
            _log.exception('Module %r failure', module)


def load_modules():
    """
    Load the configuration of things to fetch.

    In the future this will come from an external text/YAML/JSON file.
    """
    # Hard-code the modules for now.
    # TODO: Load dynamically.
    return [
        # LS5 CPF
        http.RssSource(
            'https://landsat.usgs.gov/L5CPFRSS.rss',
            '/tmp/anc/ls5-cpf'
        ),
        # LS7 CPF
        http.RssSource(
            'http://landsat.usgs.gov/L7CPFRSS.rss',
            '/tmp/anc/ls7-cpf'
        ),
        # LS8 CPF
        http.RssSource(
            'http://landsat.usgs.gov/cpf.rss',
            '/tmp/anc/ls8-cpf'
        ),
        # LS8 BPF
        http.RssSource(
            'http://landsat.usgs.gov/bpf.rss',
            '/tmp/anc/ls8-bpf/{year}/{month}',
            filename_transform=RegexpOutputPathTransform(
                # Extract year and month from filenames to use in destination directory
                'L[TO]8BPF(?P<year>[0-9]{4})(?P<month>[0-9]{2})(?P<day>[0-9]{2}).*'
            )
        ),
        # Landsat 8 TLE
        http.RssSource(
            'http://landsat.usgs.gov/exchange_cache/outgoing/TLE/TLE.rss',
            '/tmp/anc/ls8-tle/{year}',
            filename_transform=RegexpOutputPathTransform(
                # Extract year from filename. Eg:
                # 506_MOE_ACQ_2014288120000_2014288120000_2014288123117_OPS_TLE.txt
                '([A-Z0-9]+_){3}(?P<year>[0-9]{4})(?P<jul>[0-9]{3})[0-9]{6}.*_OPS_TLE.txt'
            )
        ),
        # utcpole/leapsec
        http.HttpSource(
            [
                'http://oceandata.sci.gsfc.nasa.gov/Ancillary/LUTs/modis/utcpole.dat',
                'http://oceandata.sci.gsfc.nasa.gov/Ancillary/LUTs/modis/leapsec.dat'
            ],
            '/tmp/anc'
        ),
        # Water vapour
        ftp.FtpListingSource(
            'ftp.cdc.noaa.gov',
            source_dir='/Datasets/ncep.reanalysis/surface',
            name_pattern='pr_wtr.eatm.[0-9]{4}.nc',
            target_dir='/tmp/anc/vapour'
        ),
        # GDAS and forecast
        http.DateRangeSource(
            http.HttpListingSource(
                source_url='',
                target_dir='',

                listing_name_filter='(gdas.*\\.npoess\\.grib2|NISE.*HDFEOS|gfs\\.press_gr.*grib2)'
            ),
            source_url='http://jpssdb.ssec.wisc.edu/ancillary/{year}_{month}_{day}_{julday}',
            target_dir='/tmp/anc/gdas/{year}_{month}_{day}_{julday}'
        ),
        # LUTS
        http.HttpListingSource(
            source_url='http://jpssdb.ssec.wisc.edu/ancillary/LUTS_V_1_3',
            target_dir='/tmp/anc/luts'
        ),
        ftp.FtpSource(
            hostname='is.sci.gsfc.nasa.gov',
            source_paths=[
                '/ancillary/ephemeris/tle/dr1.tle',
                '/ancillary/ephemeris/tle/norad.tle',
                '/ancillary/ephemeris/tle/noaa/noaa.tle',
            ],
            target_dir='/tmp/anc/tle'
        )
    ]


def _run():
    """
    Fetch each configured ancillary file.
    """
    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s %(message)s",
                        stream=sys.stderr, level=logging.WARNING)
    _log.setLevel(logging.DEBUG)
    logging.getLogger('onreceipt').setLevel(logging.DEBUG)

    modules = load_modules()
    execute_modules(modules)


if __name__ == '__main__':
    _run()
