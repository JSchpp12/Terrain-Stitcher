@REM terrain_stitcher create-bounds --lat 39.215556 --lon -82.220556 -t POINT

terrain_stitcher create-bounds --lat 39.794502 --lon -105.76389 -t POINT -r 20

terrain_stitcher gather-ortho -s Shape.json -o data/colorado

terrain_stitcher prep-ortho -i data/colorado -o finalDownloads -s 0.1 -e data/geoTiffs -s Shape.json