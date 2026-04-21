terrain_stitcher create-bounds --lat=35.785236 --lon=-105.78209 -t POINT -r 11

terrain_stitcher gather-ortho -s Shape.json -o downloads/nm/ortho

terrain_stitcher prep-ortho -i downloads/nm/ortho -o tesuque_peak_full -f 1.0 -e downloads/nm/geoTiff -s Shape.json