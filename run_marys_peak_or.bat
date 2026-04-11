terrain_stitcher create-bounds --lat=44.5074 --lon=-123.572426 -t POINT -r 15

terrain_stitcher gather-ortho -s Shape.json -o downloads/or/ortho

terrain_stitcher prep-ortho -i downloads/or/ortho -o marys_peak_or_small -f 0.1 -e downloads/or/geoTiff -s Shape.json