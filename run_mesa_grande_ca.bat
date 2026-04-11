terrain_stitcher create-bounds --lat=33.18885 --lon=-116.76065 -t POINT -r 15

terrain_stitcher gather-ortho -s Shape.json -o downloads/ca/ortho

terrain_stitcher prep-ortho -i downloads/ca/ortho -o mesa_grande_ca_small -f 0.1 -e downloads/ca/geoTiff -s Shape.json