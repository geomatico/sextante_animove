import os
import re
from PyQt4 import QtGui
from PyQt4.QtCore import *
from PyQt4.QtGui import *
from qgis.core import *
from sextante.parameters.ParameterVector import ParameterVector
from sextante.core.QGisLayers import QGisLayers
from sextante.outputs.OutputVector import OutputVector
from animoveAlgorithm import AnimoveAlgorithm
from sextante.parameters.ParameterSelection import ParameterSelection
from sextante.outputs.OutputRaster import OutputRaster
from sextante.core.SextanteLog import SextanteLog
from sextante.parameters.ParameterBoolean import ParameterBoolean


try:  # qgis 1.8 sextante 1.08
    from sextante.ftools import ftools_utils
except:
    from sextante.algs.ftools import ftools_utils

from sextante.parameters.ParameterTableField import ParameterTableField
from sextante.parameters.ParameterNumber import ParameterNumber
import numpy as np
from osgeo import gdal, osr
import datetime
from sextante.core.SextanteUtils import SextanteUtils

try:
    from scipy.stats.kde import gaussian_kde
    _HAS_SCIPY = True
except ImportError:
    _HAS_SCIPY = False

try:
    from statsmodels.nonparametric import kernel_density
    _HAS_STATSMODELS = True
except ImportError:
    _HAS_STATSMODELS = False


class kernelDensity(AnimoveAlgorithm):

    # Input names
    INPUT = "INPUT"
    OUTPUT = "OUTPUT"
    FIELD = "FIELD"
    PERCENT = "PERCENT"
    RESOLUTION = "RESOLUTION"
    ADD_RASTER_OUTPUTS = "ADD_RASTER_OUTPUTS"
    BW_METHOD = "BW_METHOD"
    BW_VALUE = "BW_VALUE"

    # Bandwidth method indexes
    BW_METHOD_DEFAULT = 0
    BW_METHOD_SCOTT = 1
    BW_METHOD_SILVERMAN = 2
    BW_METHOD_CV_ML = 3
    BW_METHOD_CV_LS = 4
    BW_METHOD_CUSTOM = 5

    # Bandwidth method name array
    BW_METHODS = {}

    if _HAS_STATSMODELS:
        BW_METHODS[BW_METHOD_DEFAULT] = "Rule of thumb (default)"
        BW_METHODS[BW_METHOD_CV_ML] = "Cross validation maximum likelihood"
        BW_METHODS[BW_METHOD_CV_LS] = "Cross validation least squares"

    if _HAS_SCIPY:
        BW_METHODS[BW_METHOD_SCOTT] = "Rule of thumb (Scott)"
        BW_METHODS[BW_METHOD_SILVERMAN] = "Rule of thumb (Silverman)"

    if (_HAS_STATSMODELS or
            (_HAS_SCIPY and 'set_bandwidth' in dir(gaussian_kde))):
        BW_METHODS[BW_METHOD_CUSTOM] = "Custom value"

    def getIcon(self):
        return QtGui.QIcon(os.path.dirname(__file__)
                           + "/icons/kernelDensity.png")

    def processAlgorithm(self, progress):
        currentPath = os.path.dirname(os.path.abspath(__file__))

        # Get parameters
        perc = self.getParameterValue(kernelDensity.PERCENT)
        field = self.getParameterValue(kernelDensity.FIELD)
        inputLayer = QGisLayers.getObjectFromUri(
                            self.getParameterValue(kernelDensity.INPUT))
        resolution = self.getParameterValue(kernelDensity.RESOLUTION)
        bw_method = kernelDensity.BW_METHODS.keys()[
                        self.getParameterValue(kernelDensity.BW_METHOD)]
        addRasterOutputs = self.getParameterValue(
                            kernelDensity.ADD_RASTER_OUTPUTS)

        # Adjust parameters if necessary
        if perc > 100:
            perc = 100
        if bw_method == kernelDensity.BW_METHOD_DEFAULT:
            bandwidth = 'normal_reference'
        elif bw_method == kernelDensity.BW_METHOD_SCOTT:
            bandwidth = 'scott'
        elif bw_method == kernelDensity.BW_METHOD_SILVERMAN:
            bandwidth = 'silverman'
        elif bw_method == kernelDensity.BW_METHOD_CV_ML:
            bandwidth = 'cv_ml'
        elif bw_method == kernelDensity.BW_METHOD_CV_LS:
            bandwidth = 'cv_ls'
        elif bw_method == kernelDensity.BW_METHOD_CUSTOM:
            if _HAS_SCIPY and 'set_bandwidth' in dir(gaussian_kde):
                bandwidth = self.getParameterValue(kernelDensity.BW_VALUE)
            else:
                bandwidth = [self.getParameterValue(kernelDensity.BW_VALUE)]

        # Get layer info and create the writer for the output layer
        epsg = inputLayer.crs().srsid()
        name = inputLayer.name()
        inputProvider = inputLayer.dataProvider()
        try:
            inputProvider.select(inputProvider.attributeIndexes())
        except:
            pass

        fieldIndex = inputProvider.fieldNameIndex(field)
        uniqueValues = ftools_utils.getUniqueValues(inputProvider, fieldIndex)

        fields = [QgsField("ID", QVariant.String),
                  QgsField("Area", QVariant.Double),
                  QgsField("Perim", QVariant.Double)]
        writer = self.getWriter(kernelDensity.OUTPUT, fields,
                    QGis.WKBMultiLineString, inputProvider.crs())

        # Prepare percentage progress and start
        progress_perc = 100 / len(uniqueValues)
        n = 0
        for value in uniqueValues:
            # Filter x,y points with desired field value (value)
            xPoints = []
            yPoints = []
            for feature in QGisLayers.features(inputLayer):
                fieldValue = self.getFeatureAttributes(feature)[fieldIndex]
                if (fieldValue.toString().trimmed() ==
                            value.toString().trimmed()):
                    points = ftools_utils.extractPoints(feature.geometry())
                    xPoints.append(points[0].x())
                    yPoints.append(points[0].y())

            if len(xPoints) == 0:  # number of selected features
                continue

            # Compute kernel (X, Y, Z)
            xmin = min(xPoints) - 0.5 * (max(xPoints) - min(xPoints))
            xmax = max(xPoints) + 0.5 * (max(xPoints) - min(xPoints))
            ymin = min(yPoints) - 0.5 * (max(yPoints) - min(yPoints))
            ymax = max(yPoints) + 0.5 * (max(yPoints) - min(yPoints))

            # X, Y form a meshgrid
            X, Y = np.mgrid[xmin:xmax:complex(resolution),
                            ymin:ymax:complex(resolution)]

            SextanteLog.addToLog(SextanteLog.LOG_INFO, "X shape : "
               + str(X.shape))

            SextanteLog.addToLog(SextanteLog.LOG_INFO, "Y shape : "
               + str(Y.shape))

            # Meshgrid in form of stacked array with all possible positions
            positions = np.vstack([X.ravel(), Y.ravel()])

            # Meshgrid with all the real positions
            values = np.vstack([xPoints, yPoints])

            SextanteLog.addToLog(SextanteLog.LOG_INFO, "Positions shape : "
               + str(positions.shape))

            SextanteLog.addToLog(SextanteLog.LOG_INFO, "Values shape : "
               + str(values.shape))

            if self.use_scipy(bw_method):
                ###############################################################
                # Compute kernel Z with scipy.stats.kde.gaussian_kde
                # Representation of a kernel-density estimate using Gaussian
                # kernels.
                # Taken from http://docs.scipy.org/doc/scipy/reference/
                #            generated/scipy.stats.gaussian_kde.html
                # You need at least version 0.11 of scipy
                ###############################################################
                kernel = gaussian_kde(values)
                try:
                    kernel.set_bandwidth(bandwidth)
                except:
                    if bandwidth == 'scott':
                        kernel.covariance_factor = kernel.scotts_factor
                    elif bandwidth == 'silverman':
                        kernel.covariance_factor = kernel.silverman_factor
                Z = np.reshape(kernel(positions).T, X.T.shape)
                SextanteLog.addToLog(SextanteLog.LOG_INFO,
                    "Bandwidth value for '"
                    + str(value.toString().trimmed()) + "': "
                    + str(kernel.covariance_factor()))
            else:
                ##############################################################
                # Compute kernel Z with
                #    statsmodels.nonparametric.kernel_density.KDEMultivariate
                # Representation of a kernel-density estimate
                # Taken from http://goo.gl/kwEfD
                # You need at least version 0.5 of statsmodels
                ##############################################################
                kernel = kernel_density.KDEMultivariate(data=values,
                                var_type='cc', bw=bandwidth)
                # Evaluate positions using kernel
                Z = np.reshape(kernel.pdf(positions).T, X.T.shape)
                SextanteLog.addToLog(SextanteLog.LOG_INFO,
                    "Bandwidth value for: "
                    + str(value.toString().trimmed())
                    + "': " + str(kernel.bw))

            SextanteLog.addToLog(SextanteLog.LOG_INFO,
                "Shape of evaluation transponse : " + str(Z.T.shape))

            # Write kernel to GeoTIFF
            raster_name = (str(name) + '_' + str(perc) + '_' +
                        str(value.toString()) + '_' +
                        str(datetime.date.today()))
            fileName = currentPath + '/raster_output/' + raster_name

            SextanteLog.addToLog(SextanteLog.LOG_INFO, "Writing '"
               + fileName + "' to disc")

            self.to_geotiff(fileName, xmin, xmax, ymin, ymax, X, Y, Z, epsg)

            if addRasterOutputs:
                SextanteLog.addToLog(SextanteLog.LOG_INFO,
                        "Adding raster output as layer")
                rasterOutput = OutputRaster(fileName, "Raster output")
                self.addOutput(rasterOutput)
                rasterOutput.setValue(fileName)

            # Create contour lines (temporary .shp) from GeoTIFF
            if SextanteUtils.isWindows():
                cmd = "gdal_contour.exe "
            else:
                cmd = "gdal_contour "

            basename = "c" + str(n)
            shpFile = os.path.join(currentPath, basename + ".shp")

            contour_cmd = (cmd + currentPath + "/raster_output/"
                          + raster_name + " -a values -i 10 " + shpFile)

            SextanteLog.addToLog(SextanteLog.LOG_INFO,
                    "Creating contour lines for GeoTIFF: " + contour_cmd)

            os.system(contour_cmd)

            # Read contour lines from temporary .shp
            SextanteLog.addToLog(SextanteLog.LOG_INFO, "Reading contour lines "
                    + " from temporary SHP: "
                    + shpFile)

            layer = QgsVectorLayer(shpFile, basename, "ogr")
            provider = layer.dataProvider()
            try:
                provider.select(provider.attributeIndexes())
            except:
                pass

            # Create an array containing all polylines in the temporary
            # .shp and compute the sum of all areas and perimeters
            SextanteLog.addToLog(SextanteLog.LOG_INFO, "Creating polylines "
                    + " from all SHP")
            outGeom = []
            area = 0
            perim = 0
            measure = QgsDistanceArea()
            features = QGisLayers.features(layer)
            for feat in features:
                polyline = feat.geometry().asPolyline()
                polygon = QgsGeometry.fromPolygon([polyline])
                perim += measure.measurePerimeter(polygon)
                area += measure.measure(polygon)
                outGeom.append(polyline)

            # Create feature and write
            SextanteLog.addToLog(SextanteLog.LOG_INFO,
                                 "Writing polylines features")
            outFeat = QgsFeature()
            outFeat.setGeometry(QgsGeometry.fromMultiPolyline(outGeom))
            self.setFeatureAttributes(feature, [value.toString(), area, perim])
            writer.addFeature(outFeat)

            SextanteLog.addToLog(SextanteLog.LOG_INFO, "Removing temporary "
                    + "files and updating progress bar")

            # Remove temporary files and update progress
            for f in os.listdir(currentPath):
                if re.search(basename + ".*", f):
                    os.remove(os.path.join(currentPath, f))
            n += 1
            progress.setPercentage(progress_perc * n)

        SextanteLog.addToLog(SextanteLog.LOG_INFO, "Finished. Deleting writer")
        del writer

    def defineCharacteristics(self):
        self.name = "Kernel Density Estimation"
        self.group = "Tools"
        self.addParameter(ParameterVector(kernelDensity.INPUT, "Input layer",
                    ParameterVector.VECTOR_TYPE_POINT))
        self.addParameter(ParameterTableField(kernelDensity.FIELD,
                    "Group fixes by", kernelDensity.INPUT))
        self.addParameter(ParameterNumber(kernelDensity.PERCENT,
                    "Percentage of Utilization Distribution (UD)", 5, 100, 95))
        self.addParameter(ParameterNumber(kernelDensity.RESOLUTION,
                    "Output raster resolution", 1, None, 50))
        self.addParameter(ParameterSelection(kernelDensity.BW_METHOD,
                    "Bandwidth method", kernelDensity.BW_METHODS.values()))
        if (_HAS_STATSMODELS or
            (_HAS_SCIPY and 'set_bandwidth' in dir(gaussian_kde))):
            self.addParameter(ParameterNumber(kernelDensity.BW_VALUE,
                        "Bandwidth value (only used  if 'Custom value' "
                        "bandwidth method selected)", 0.0, None, 0.2))
        self.addParameter(ParameterBoolean(kernelDensity.ADD_RASTER_OUTPUTS,
                    "Add raster outputs to QGIS"))
        self.addOutput(OutputVector(kernelDensity.OUTPUT,
                    "Kernel Density Estimation"))

    def to_geotiff(self, fname, xmin, xmax, ymin, ymax, X, Y, Z, epsg):
        '''
        saves the kernel as a GEOTIFF image
        '''
        driver = gdal.GetDriverByName("GTiff")
        out = driver.Create(fname, len(X), len(Y), 1, gdal.GDT_Float64)

        # pixel sizes
        xps = (xmax - xmin) / float(len(X))
        yps = (ymax - ymin) / float(len(Y))
        out.SetGeoTransform((xmin, xps, 0, ymin, 0, yps))
        coord_system = osr.SpatialReference()
        coord_system.ImportFromEPSG(epsg)
        out.SetProjection(coord_system.ExportToWkt())

        Z = Z.clip(0) * 100.0 / Z.max()

        out.GetRasterBand(1).WriteArray(Z.T)

    def use_scipy(self, bw_method):
        if not _HAS_SCIPY:
            return False

        return (bw_method == kernelDensity.BW_METHOD_SCOTT or
                bw_method == kernelDensity.BW_METHOD_SCOTT or
                (bw_method == kernelDensity.BW_METHOD_CUSTOM and
                    'set_bandwidth' in dir(gaussian_kde)))
