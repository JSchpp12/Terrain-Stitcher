terrain_stitcher create-bounds --lat 39.215556 --lon -82.220556 -t POINT

terrain_stitcher gather-ortho -s Shape.json

terrain_stitcher prep-ortho -i tmpDownloads -o finalDownloads