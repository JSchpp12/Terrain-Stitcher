@REM terrain_stitcher create-bounds --lat=34.91022 --lon=-88.551346 -t POINT -r 11

@REM terrain_stitcher gather-ortho -s Shape.json -o downloads/ms/ortho

terrain_stitcher prep-ortho -i downloads/ms/ortho -o corinth_ms_small -f 0.1 -e downloads/ms/geoTiff -s Shape.json