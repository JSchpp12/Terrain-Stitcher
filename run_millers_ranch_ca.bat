terrain_stitcher create-bounds --lat=36.2515 --lon=-121.4523 -t POINT -r 15

terrain_stitcher gather-ortho -s Shape.json -o downloads/ca/ortho

terrain_stitcher prep-ortho -i downloads/ca/ortho -o millers_ranch_ca_small -f 0.1 -e downloads/ca/geoTiff -s Shape.json