terrain_stitcher download-arcgis -s Shape.json --lod 18 -o output_tiles_18

terrain_stitcher gather-ortho -src arcgis -i output_tiles_18 --from-download -o perryville_18_small -d 75 -f 0.05 --lod_min 18

terrain_stitcher gather-ortho -src arcgis -i output_tiles_18 --from-download -o perryville_18_full -d 75 --lod_min 18

terrain_stitcher download-arcgis -s Shape.json --lod 19 -o output_tiles_19

terrain_stitcher gather-ortho -src arcgis -i output_tiles_19 --from-download -o perryville_19_full --lod_min 19