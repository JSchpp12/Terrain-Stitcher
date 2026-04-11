@REM terrain_stitcher create-bounds --lat 39.215556 --lon -82.220556 -t POINT

@REM alaska
terrain_stitcher create-bounds --lat 58.36555 --lon -134.63495 -t POINT -r 20

terrain_stitcher gather-ortho -s Shape.json -o downloads/alaska_new/ortho

terrain_stitcher prep-ortho -i downloads/alaska_new/ortho -o alaska_small -f 0.1 -e downloads/alaska/geoTiff -s Shape.json