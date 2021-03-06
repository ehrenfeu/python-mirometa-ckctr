#!/usr/bin/python

"""Classes to handle various types of datasets."""

import codecs
import string    # bug #2481 pylint: disable=deprecated-module
import ConfigParser
import xml.etree.ElementTree as etree
from io import StringIO

import olefile

from log import log
from .pathtools import parse_path, exists


class DataSet(object):  # pylint: disable=too-few-public-methods

    """The most generic dataset object, to be subclassed and specialized."""

    def __init__(self, ds_type, st_type, st_path):
        """Prepare the dataset object.

        Parameters
        ----------
        ds_type : str
            One of ('mosaic', 'stack', 'single')
        st_type : str
            'single' : a single file container with the full dataset
            'tree' : a directory hierarchy
            'sequence' : a sequence of files
        st_path : str
            The full path to either a file or directory, depending on the
            storage type of this dataset.
        supplement : dict
            An auxiliary dict to keep supplementary information.

        Instance Variables
        ------------------
        ds_type : str
        storage : pathtools.parse_path
        """
        log.debug("Creating a 'Dataset' object.")
        ds_type_allowed = ('mosaic', 'stack', 'single')
        st_type_allowed = ('single', 'tree', 'sequence')
        if not ds_type in ds_type_allowed:
            raise TypeError("Illegal dataset type: %s." % ds_type)
        if not st_type in st_type_allowed:
            raise TypeError("Illegal storage type: %s." % st_type)
        self.ds_type = ds_type
        self.storage = parse_path(st_path)
        self.storage['type'] = st_type
        if st_type == 'single' and self.storage['fname'] == '':
            raise TypeError("File name missing for storage type 'single'.")
        self.supplement = {}


class ImageData(DataSet):

    """Specific DataSet class for images, 2D to 5D."""

    def __init__(self, ds_type, st_type, st_path):
        """Set up the image dataset object.

        Parameters
        ----------
        ds_type : str
            One of ('mosaic', 'stack', 'single')
        st_type : str
            'single' : a single file container with the full dataset
            'tree' : a directory hierarchy
            'sequence' : a sequence of files
        st_path : str
            The full path to either a file or directory, depending on the
            storage type of this dataset.

        Instance Variables
        ------------------
        _dim = {
            'B': int,  # bit depth
            'C': int,  # channels
            'T': int,  # timepoints
            'X': int,
            'Y': int,
            'Z': int
        }
        position : dict
            Spatial information for multi-image datasets:
            {
                'stage' : (float, float),    # raw stage coords
                'relative' : (float, float)  # relative coords in pixels
            }
        """
        super(ImageData, self).__init__(ds_type, st_type, st_path)
        log.debug("Creating an 'ImageData' object.")
        log.debug("ds_type: '%s'", self.ds_type)
        # TODO: convert "_dim" to property
        self._dim = {
            'B': 0,  # bit depth
            'C': 0,  # channels
            'T': 0,  # timepoints
            'X': 0,
            'Y': 0,
            'Z': 0
        }
        self.position = {      # spatial information for multi-image datasets
            'stage': None,    # raw stage coordinates
            'relative': None  # relative coordinates in pixel values (float)
        }

    def set_stagecoords(self, coords):
        """Set the stageinfo coordinates for this object."""
        log.debug("Setting stage coordinates: %s.", str(coords))
        self.position['stage'] = coords

    def set_relpos(self, overlap):
        """Calculate the relative coordinates from the tile overlap."""
        raise NotImplementedError('set_relpos() not implemented in base class')

    def set_tilenumbers(self, tileno_x, tileno_y, tileno_z=None):
        """Set the tile index number in the supplementary informations."""
        log.debug("Tile grid indices x / y / z: %s / %s / %s",
                  tileno_x, tileno_y, tileno_z)
        self.supplement['tileno'] = (tileno_x, tileno_y, tileno_z)

    def get_dimensions(self):
        """Lazy parsing of the image dimensions."""
        raise NotImplementedError('get_dimensions() not implemented!')


class ImageDataOlympus(ImageData):

    """Meta DataSet class for images in one of the Olympus file formats."""

    def __init__(self, st_path):
        """Set up the image dataset object.

        Parameters
        ----------
        st_path : str
            The full path to the dataset file.

        Instance Variables
        ------------------
        For inherited variables, see ImageData.
        """
        super(ImageDataOlympus, self).__init__('stack', 'tree', st_path)
        self.storage = self.validate_filepath()
        self.parser = None  # needs to be done in the subclass
        self._dim = None  # override _dim to mark it as not yet known

    def validate_filepath(self):
        """Fix the broken filenames in FluoView experiment files.

        The FluoView software usually stores corrupted filenames in its
        experiment description files, that have a missing suffix, e.g.

            Slide1sec001\\Slide1sec001.oib

        whereas the correct filename would be

            Slide1sec001\\Slide1sec001_01.oib

        This function attempts to fix this by checking if the supplied path is
        actually existing and trying the default suffix if not. Raises an
        IOError exception if no corresponding file can be found.

        Returns
        -------
        storage : pathtools.parse_path
        """
        fpath = self.storage
        ext = fpath['ext']
        log.debug("Validating file path: %s", fpath)
        if not exists(fpath['full']):
            fpath = parse_path(fpath['orig'].replace(ext, '_01' + ext))
            log.debug("Trying next path: %s", fpath['full'])
        if not exists(fpath['full']):
            raise IOError("Can't find file: %s" % fpath)
        return fpath

    def parse_dimensions(self):
        """Read image dimensions from a ConfigParser object.

        Returns
        -------
        dim : {
            'X': int,
            'Y': int,
            'Z': int,
            'C': int,  # channels
            'T': int,  # timepoints
            'B': int   # bit depth
        }
            Pixel dimensions in X and Y direction as tuple.
        """
        get = self.parser.get
        try:
            dim_b = get(u'Reference Image Parameter', u'ValidBitCounts')
            dim_x = get(u'Reference Image Parameter', u'ImageHeight')
            dim_y = get(u'Reference Image Parameter', u'ImageWidth')
            dim_z = get(u'Axis 3 Parameters Common', u'MaxSize')
            axis_z = get(u'Axis 3 Parameters Common', u'AxisName')
            dim_c = get(u'Axis 2 Parameters Common', u'MaxSize')
            axis_c = get(u'Axis 2 Parameters Common', u'AxisName')
            dim_t = get(u'Axis 4 Parameters Common', u'MaxSize')
            axis_t = get(u'Axis 4 Parameters Common', u'AxisName')
        except ConfigParser.NoOptionError as err:
            raise ValueError("Error parsing dimensions from %s: %s" %
                             (self.storage['full'], err))
        # check if we got the right axis for Z/Ch/T, set to 0 otherwise:
        if not axis_z == u'"Z"':
            log.warn("WARNING: couldn't find Z axis in metadata!")
            dim_z = 0
        if not axis_c == u'"Ch"':
            log.warn("WARNING: couldn't find channels in metadata!")
            dim_c = 0
        if not axis_t == u'"T"':
            log.warn("WARNING: couldn't find timepoints in metadata!")
            dim_t = 0
        dim = {
            'B': int(dim_b),  # bit depth
            'C': int(dim_c),  # channels
            'T': int(dim_t),  # timepoints
            'X': int(dim_x),
            'Y': int(dim_y),
            'Z': int(dim_z)
        }
        log.info('Parsed image dimensions: %s', dim)
        return dim

    def get_dimensions(self):
        """Lazy parsing of the image dimensions."""
        if self._dim is None:
            self._dim = self.parse_dimensions()
        return self._dim

    def set_relpos(self, overlap):
        """Calculate the relative coordinates from the tile overlap.

        Parameters
        ----------
        overlap : float
            The overlap between tiles in percent.
        """
        ratio = (100.0 - overlap) / 100
        size_x = self.get_dimensions()['X']
        size_y = self.get_dimensions()['Y']
        tileno_x = self.supplement['tileno'][0]
        tileno_y = self.supplement['tileno'][1]
        pos_x = size_x * ratio * tileno_x
        pos_y = size_y * ratio * tileno_y
        log.info("Setting relative coordinates: %s, %s.", pos_x, pos_y)
        self.position['relative'] = (pos_x, pos_y)


class ImageDataOIF(ImageDataOlympus):

    """Specific DataSet class for images in Olympus OIF format."""

    def __init__(self, st_path):
        """Set up the image dataset object.

        Parameters
        ----------
        st_path : str
            The full path to the .OIF file.

        Instance Variables
        ------------------
        For inherited variables, see ImageData.
        """
        log.debug("ImageDataOIF(%s)", st_path)
        super(ImageDataOIF, self).__init__(st_path)
        self.parser = self.setup_parser()
        self._dim = None  # override _dim to mark it as not yet known

    def setup_parser(self):
        """Set up the ConfigParser object for this .oif file.

        Use the 'codecs' package to set up a ConfigParser object that can
        properly handle the UTF-16 encoded .oif files.
        """
        # TODO: investigate usage of 'io' package instead of 'codecs'
        oif = self.storage['full']
        log.info('Parsing OIF file: %s', oif)
        try:
            conv = codecs.open(oif, "r", "utf16")
        except IOError:
            raise IOError("Error parsing OIF file (does it exist?): %s" % oif)
        parser = ConfigParser.RawConfigParser()
        parser.readfp(conv)
        conv.close()
        log.debug('Finished parsing OIF file.')
        return parser


class ImageDataOIB(ImageDataOlympus):

    """Specific DataSet class for images in Olympus OIB format."""

    def __init__(self, st_path):
        """Set up the image dataset object.

        Parameters
        ----------
        st_path : str
            The full path to the .OIB file.

        Instance Variables
        ------------------
        For inherited variables, see ImageDataOlympus (and ImageData).
        """
        log.debug("ImageDataOIB(%s)", st_path)
        super(ImageDataOIB, self).__init__(st_path)
        self.parser = self.setup_parser()

    def setup_parser(self):
        """Set up the ConfigParser object for this .oib file.

        Use the 'olefile' package to open the OIB container file, read in
        the description file (using the 'codecs' package to properly handle the
        UTF-16 encoding). Some minor checks on the description file are done
        where also the "main" file of the OIB container (containing all the
        metadata like dimensions, channels, etc.) is identified and eventually
        the parser is set up for this file.

        Jython Debugging
        ================
        As Java has quite an influence on the binary parsing in the "olefile"
        package, it is sometimes necessary to do some manual debugging:

        >>> import sys
        >>> import codecs
        >>> sys.path.insert(0, PATH_TO_OLEFILE_PACKAGE)
        >>> import olefile
        >>> ole = olefile.OleFileIO(PATH_TO_OIB_FILE)
        >>> ole = olefile.OleFileIO(PATH_TO_OIB_FILE, debug=True)
        """
        oibinfo = 'OibInfo.txt'
        encoding = 'utf16'
        expected_version = '2.0.0.0'
        # TODO: investigate usage of 'io' package instead of 'codecs'
        oib = self.storage['full']
        log.info('Parsing OIB file: %s', oib)
        try:
            ole = olefile.OleFileIO(oib)
        except IOError as err:
            raise IOError("Error parsing OIB file: %s" % err)
        log.info('Parsing OIB description file "%s".', oibinfo)
        try:
            stream = ole.openstream([oibinfo])
        except IOError as err:
            raise IOError("OIB description (%s) missing: %s" % (oibinfo, err))
        try:
            conv = codecs.decode(stream.read(), encoding)
        except UnicodeDecodeError as err:
            raise UnicodeDecodeError("OIB has unexpected encoding: %s" % err)
        parser = ConfigParser.RawConfigParser()
        parser.readfp(StringIO(conv))
        oibver = parser.get(u'OibSaveInfo', u'Version')
        mainfile = parser.get(u'OibSaveInfo', u'MainFileName')
        if oibver != expected_version:
            log.warn('WARNING: OIB has unknown format version %s!', oibver)
        else:
            log.info('OIB Format Version: %s', oibver)
        log.debug('Main File Name: %s', mainfile)
        stream.close()
        log.info('Finished parsing OIB description file.')
        # replace stream and parser with the mainfile:
        stream = ole.openstream([mainfile])
        conv = codecs.decode(stream.read(), encoding)
        parser.readfp(StringIO(conv))
        # clean up and return the parser:
        log.debug('Finished parsing OIB file.')
        stream.close()
        ole.close()
        return parser


class ImageDataOIR(ImageDataOlympus):

    """Dataset class for the Olympus OIR format."""

    def __init__(self, st_path):
        """Set up the image dataset object.

        Parameters
        ----------
        st_path : str
            The full path to the .OIR file.

        Instance Variables
        ------------------
        For inherited variables, see ImageDataOlympus (and ImageData).
        """
        log.debug("ImageDataOIR(%s)", st_path)
        super(ImageDataOIR, self).__init__(st_path)
        # XML namespace definitions required for parsers:
        ns_base = 'http://www.olympus.co.jp/hpf'
        self._xmlns = {
            'base': '%s/model/base' % ns_base,
            'commonframe': '%s/model/commonframe' % ns_base,
            'commonimage': '%s/model/commonimage' % ns_base,
            'commonparam': '%s/model/commonparam' % ns_base,
        }
        self._xml = self.get_xml_sections()
        self.parse_dimensions()
        ### self.parser = self.setup_parser()

    def get_xml_sections(self, min_len=100):
        """Scan the OIR file for strings containing specific XML structures.

        Read in the file (in chunks of a defined size, to save memory) and scan
        for sequences of printable chars. If a sequence exceeds a given minimum
        length, check if it is an XML structure with a specific substring. Add
        all sections found to a dict and return it.

        Parameters
        ----------
        min_len : int
            Minimum length of a sequence to be checked for being the wanted XML.

        Returns
        -------
        found : dict
            A dict containing the found XML sections in one string per key.
        """
        count = 0
        size = 1048576  # set chunk size to be 1 MiB
        collected = ''
        found = dict()
        search_tags = [
            'lsmframe:frameProperties',
            'lsmimage:imageProperties',
        ]

        with open(self.storage['full'], 'rb') as fin:
            while True:
                chunk = fin.read(size)
                # raise an exception if we reach EOF and haven't found all tags:
                if not chunk:
                    log.debug("Read %s bytes in %s chunks.", count*size, count)
                    raise ValueError("Couldn't find all requested XML blocks!")
                count += 1

                for char in chunk:
                    # collect sequences of printable chars:
                    if char in string.printable:
                        collected += char
                        continue

                    # if the sequence is below a minimum length or it doesn't
                    # contain an XML block discard it and proceed with next:
                    if len(collected) < min_len or '<?xml' not in collected:
                        collected = ''
                        continue

                    # check if sequence contains any of the searched XML tags:
                    for tag in search_tags:
                        if '<' + tag in collected:
                            log.debug('Found <%s> XML section.', tag)
                            xml_close = collected.rfind('>') + 1
                            if len(collected) - xml_close > 0:
                                log.debug('Stripping %s trailing chars: "%s"',
                                          len(collected) - xml_close,
                                          collected[xml_close:])
                            found[tag] = collected[:xml_close]
                            # stop once all searched tags were found:
                            if len(found) == len(search_tags):
                                log.debug('Stopping after %s bytes.',
                                          count * size)
                                return found

                    # reset collected chars for next round:
                    collected = ''

    def parse_dimensions(self):
        """Wrapper to call the various specialized XML parsers."""
        self._dim = {
            'X': 0,
            'Y': 0,
            'Z': 0,
            'C': 0,  # channels
            'B': 0,  # bit depth
            'T': 0,  # timepoints
        }
        try:
            self._parse_frameprops(self._xml['lsmframe:frameProperties'])
            self._parse_imageprops(self._xml['lsmimage:imageProperties'])
        except:
            log.error('Error parsing dimensions from %s!', self.storage['full'])
            raise
        log.debug('Parsed dimensions: x=%s y=%s z=%s bit=%s c=%s t=%s',
                  self._dim['X'], self._dim['Y'], self._dim['Z'],
                  self._dim['B'], self._dim['C'], self._dim['T'])

    def _parse_frameprops(self, xml):
        """Parse X/Y dimensions and bit-depth from frameProperties XML."""
        # lambda functions for tree.find().text and int/float conversions:
        tft = lambda t, p: t.find(p, self._xmlns).text
        tfi = lambda t, p: int(tft(t, p))

        log.debug('Trying to parse frameProperties XML...')
        fp_root = etree.fromstring(xml)
        img_def = fp_root.find('commonframe:imageDefinition', self._xmlns)

        self._dim['X'] = tfi(img_def, 'base:width')
        self._dim['Y'] = tfi(img_def, 'base:height')
        self._dim['B'] = tfi(img_def, 'base:bitCounts')

    def _parse_imageprops(self, xml):
        """Parse Z dimension from imageProperties XML.

        Information about the number of Z-sections, channels and timepoints is
        stored in the 'imageProperties' XML block. Currently only the Z
        dimension is parsed and added to the common dict.
        """
        # TODO: implement parsing of C and T
        # XML schema instance
        xsi = '{http://www.w3.org/2001/XMLSchema-instance}'

        # lambda functions for tree.find().text and int/float conversions:
        tft = lambda t, p: t.find(p, self._xmlns).text
        tfi = lambda t, p: int(tft(t, p))

        log.debug('Trying to parse imageProperties XML...')
        dim_z = 0  # set default to zero in case this is not a z-stack
        ip_root = etree.fromstring(xml)
        ci_acq = ip_root.find('commonimage:acquisition', self._xmlns)
        ci_param = ci_acq.find('commonimage:imagingParam', self._xmlns)
        ci_axis = ci_param.findall('commonparam:axis', self._xmlns)
        log.debug('Found "commonparam:axis" subtree.')
        for axis in ci_axis:
            if axis.attrib[xsi + 'type'] == 'commonparam:ZAxisParam':
                log.debug('Found axis of type "commonparam:ZAxisParam".')
                if tft(axis, 'commonparam:paramName') == 'Start End':
                    log.debug('Found paramName "Start End" subtree.')
                    dim_z = tfi(axis, 'commonparam:maxSize')
                    log.debug('Found Z-axis size: %s', dim_z)
                    break

        self._dim['Z'] = dim_z


class MosaicData(DataSet):

    """Special DataSet class for mosaic / tiling datasets."""

    def __init__(self, st_type, st_path):
        """Set up the mosaic dataset object.

        Parameters
        ----------
        st_type, st_path : see superclass

        Instance Variables
        ------------------
        subvol : list(ImageData)
        """
        super(MosaicData, self).__init__('mosaic', st_type, st_path)
        self.subvol = list()

    def add_subvol(self, img_ds):
        """Add a subvolume to this dataset.

        Parameters
        ----------
        img_ds : ImageData
            An ImageData object representing the subvolume.
        """
        log.debug('Dataset type: %s', type(img_ds))
        self.subvol.append(img_ds)


class MosaicDataCuboid(MosaicData):

    """Special case of a full cuboid mosaic volume."""

    def __init__(self, st_type, st_path, dim):
        """Set up the mosaic dataset object.

        Parameters
        ----------
        st_type, st_path : see superclass
        dim : list(int, int, int)
            Number of sub-volumes (stacks) in all spatial dimensions.

        Instance Variables
        ------------------
        subvol : list(ImageData)
        dim = {
            'X': int,  # number of sub-volumes in X-direction
            'Y': int,  # number of sub-volumes in Y-direction
            'Z': int   # number of sub-volumes in Z-direction
        }
        """
        super(MosaicDataCuboid, self).__init__(st_type, st_path)
        log.info('Mosaic: %ix%ix%i', dim[0], dim[1], dim[2])
        self.dim = {'X': dim[0], 'Y': dim[1], 'Z': dim[2]}
        self.overlap = 0
        self.overlap_units = 'px'

    def set_overlap(self, value, units='px'):
        """Set the overlap amount and unit."""
        log.debug('Setting overlap to %s %s.', value, units)
        units_allowed = ['px', 'pct', 'um', 'nm', 'mm']
        if units not in units_allowed:
            raise TypeError('Unknown overlap unit given: %s' % units)
        # TODO: this warning should be displayed for other units as well
        if units == 'pct' and value <= 5.0:
            log.warn('Low overlap %.1f%%!', value)
        self.overlap = value
        self.overlap_units = units

    def get_overlap(self, units='pct'):
        """Get the overlap amount in a specific unit."""
        # TODO: implement conversion for other units:
        # units_allowed = ['px', 'pct', 'um', 'nm', 'mm']
        units_allowed = ['pct']
        if units not in units_allowed:
            raise TypeError('Unknown overlap unit requested: %s' % units)
        if units != self.overlap_units:
            raise NotImplementedError('Unit conversion not implemented!')
        return self.overlap
