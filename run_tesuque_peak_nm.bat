terrain_stitcher create-bounds --lat=44.5074 --lon=-123.572426 -t POINT -r 15

terrain_stitcher gather-ortho -s Shape.json -o downloads/nm/ortho

terrain_stitcher prep-ortho -i downloads/nm/ortho -o tesuque_peak_small -f 0.1 -e downloads/nm/geoTiff -s Shape.json