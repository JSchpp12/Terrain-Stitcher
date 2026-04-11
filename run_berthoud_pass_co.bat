terrain_stitcher create-bounds --lat 39.794502 --lon -105.76389 -t POINT -r 15

terrain_stitcher gather-ortho -s Shape.json -o downloads/co/ortho

terrain_stitcher prep-ortho -i downloads/co/ortho -o berthoud_pass_co_small -f 0.1 -e downloads/co/geoTiff -s Shape.json