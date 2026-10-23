"""Make a tileset atlas from a folder of tile images. Each file keeps its slot, so UVs stay correct."""
import math
import os

import bmesh
import bpy
import numpy
from bpy.path import abspath

from . import sprytile_utils

IMAGE_EXTS = {".png", ".tga", ".bmp", ".jpg", ".jpeg", ".tif", ".tiff", ".psd", ".webp"}
WATCH_INTERVAL = 1.0
# Edge pixels repeated around each tile, so filtering at a face edge does not pull in the next tile
DEFAULT_PADDING = 2


class SprytileTileSlot(bpy.types.PropertyGroup):
    # name is the file name
    stamp: bpy.props.StringProperty(
        description="File time and size at the last copy into the atlas"
    )


def list_tile_files(folder):
    if not os.path.isdir(folder):
        return []
    return sorted(
        entry.name for entry in os.scandir(folder)
        if entry.is_file() and os.path.splitext(entry.name)[1].lower() in IMAGE_EXTS
    )


def file_stamp(filepath):
    stat = os.stat(filepath)
    return "%d:%d" % (stat.st_mtime_ns, stat.st_size)


def read_image_pixels(filepath):
    """Returns width, height and an (h, w, 4) pixel array. Row 0 is the bottom row."""
    img = bpy.data.images.load(filepath, check_existing=False)
    try:
        width, height = img.size
        pixels = numpy.empty(width * height * 4, dtype=numpy.float32)
        img.pixels.foreach_get(pixels)
        return width, height, pixels.reshape(height, width, 4)
    finally:
        bpy.data.images.remove(img)


def get_folder_material(mat_data):
    idx = bpy.data.materials.find(mat_data.mat_id)
    return bpy.data.materials[idx] if idx >= 0 else None


def remap_material_uvs(material, remap_face):
    """Calls remap_face with the UVs of each face that uses the material, for each UV layer."""
    for mesh in bpy.data.meshes:
        mat_indices = {i for i, mat in enumerate(mesh.materials) if mat == material}
        if not mat_indices:
            continue
        if mesh.is_editmode:
            bm = bmesh.from_edit_mesh(mesh)
            for uv_layer in bm.loops.layers.uv.values():
                for face in bm.faces:
                    if face.material_index in mat_indices:
                        remap_face([loop[uv_layer].uv for loop in face.loops])
            bmesh.update_edit_mesh(mesh, loop_triangles=False, destructive=False)
        else:
            for uv_layer in mesh.uv_layers:
                for poly in mesh.polygons:
                    if poly.material_index in mat_indices:
                        remap_face([uv_layer.data[i].uv for i in poly.loop_indices])
            mesh.update()


def remap_rows_added(material, old_height, new_height):
    """Move UVs so that they stay on the same pixels after the atlas gets more rows at the bottom."""
    scale = old_height / new_height

    def remap(uvs):
        for uv in uvs:
            uv.y = 1.0 - (1.0 - uv.y) * scale

    remap_material_uvs(material, remap)


def remap_padding(material, tile_w, tile_h, old_pad, new_pad, old_size, new_size):
    """Move UVs to the same tile pixels after the padding around each cell changes."""
    old_w, old_h = old_size
    new_w, new_h = new_size
    old_cell = (tile_w + old_pad * 2, tile_h + old_pad * 2)
    new_cell = (tile_w + new_pad * 2, tile_h + new_pad * 2)

    def remap(uvs):
        # Pick the cell from the face centre, since corners sit on cell edges. Y counts down from the top.
        cx = sum(uv.x for uv in uvs) / len(uvs) * old_w
        cy = (1.0 - sum(uv.y for uv in uvs) / len(uvs)) * old_h
        col = min(max(int(cx // old_cell[0]), 0), old_w // old_cell[0] - 1)
        row = min(max(int(cy // old_cell[1]), 0), old_h // old_cell[1] - 1)
        for uv in uvs:
            x = uv.x * old_w - col * old_cell[0] - old_pad
            y = (1.0 - uv.y) * old_h - row * old_cell[1] - old_pad
            uv.x = (col * new_cell[0] + new_pad + x) / new_w
            uv.y = 1.0 - (row * new_cell[1] + new_pad + y) / new_h

    remap_material_uvs(material, remap)


def write_cell(pixels, height, col, row, cell_w, cell_h, pad, tile):
    """Writes a tile into its cell and repeats its edge pixels into the padding. Tile row 0 is the bottom."""
    x0 = col * cell_w
    y_top = height - row * cell_h
    if pad:
        tile = numpy.pad(tile, ((pad, pad), (pad, pad), (0, 0)), mode='edge')
    pixels[y_top - cell_h:y_top, x0:x0 + cell_w] = tile


def set_grid_padding(mat_data, tile_w, tile_h):
    pad = mat_data.atlas_padding
    for grid in mat_data.grids:
        # The padding setter shrinks the grid, so set the grid size after it
        grid.padding = (pad, pad)
        grid.grid = (tile_w, tile_h)


def get_or_create_atlas(material, width, height):
    atlas = sprytile_utils.get_material_texture(material)
    if atlas is None:
        atlas = bpy.data.images.new(material.name + " atlas", width, height, alpha=True)
        atlas.pixels.foreach_set(numpy.zeros(width * height * 4, dtype=numpy.float32))
        sprytile_utils.set_material_texture(material, atlas)
    return atlas


def sync_tile_folder(mat_data, force=False):
    """Copy new and changed files into the atlas. Returns warnings, or None if nothing changed."""
    material = get_folder_material(mat_data)
    folder = abspath(mat_data.tile_dir)
    if material is None or not os.path.isdir(folder) or len(mat_data.grids) == 0:
        return None

    tile_w, tile_h = mat_data.grids[0].grid
    pad = mat_data.atlas_padding
    cell_w, cell_h = tile_w + pad * 2, tile_h + pad * 2
    cols = mat_data.atlas_columns
    width = cols * cell_w

    slots = mat_data.tile_slots
    for name in list_tile_files(folder):
        if slots.find(name) < 0:
            slots.add().name = name

    rows = mat_data.atlas_rows
    needed_rows = max(math.ceil(len(slots) / cols), 1)
    old_height = rows * cell_h
    grew = needed_rows > rows
    if grew:
        # Add rows in blocks of atlas_columns
        rows = math.ceil(needed_rows / cols) * cols
        mat_data.atlas_rows = rows
    height = rows * cell_h

    atlas = get_or_create_atlas(material, width, height)
    resized = tuple(atlas.size) != (width, height)
    if resized:
        old_pixels = None
        if grew and tuple(atlas.size) == (width, old_height):
            old_pixels = numpy.empty(width * old_height * 4, dtype=numpy.float32)
            atlas.pixels.foreach_get(old_pixels)
        atlas.scale(width, height)
        pixels = numpy.zeros((height, width, 4), dtype=numpy.float32)
        if old_pixels is not None:
            # The bottom rows are at the start of the pixel buffer
            pixels[height - old_height:] = old_pixels.reshape(old_height, width, 4)
        else:
            force = True
    else:
        pixels = numpy.empty(width * height * 4, dtype=numpy.float32)
        atlas.pixels.foreach_get(pixels)
        pixels = pixels.reshape(height, width, 4)

    if grew and old_height > 0:
        remap_rows_added(material, old_height, height)
        added_rows = rows - old_height // cell_h
        for grid in mat_data.grids:
            grid.tile_selection[1] += added_rows

    warnings = []
    changed = resized or grew
    for slot_idx, slot in enumerate(slots):
        filepath = os.path.join(folder, slot.name)
        if not os.path.isfile(filepath):
            # Keep the slot and its pixels for faces that use it
            continue
        stamp = file_stamp(filepath)
        if not force and stamp == slot.stamp:
            continue

        try:
            img_w, img_h, image = read_image_pixels(filepath)
        except RuntimeError as err:
            warnings.append("Could not read %s: %s" % (slot.name, err))
            continue
        if img_w > tile_w or img_h > tile_h:
            warnings.append("%s is %dx%d, larger than the %dx%d tile size" % (slot.name, img_w, img_h, tile_w, tile_h))
            continue

        # Put smaller images in the top left of the slot
        tile = numpy.zeros((tile_h, tile_w, 4), dtype=numpy.float32)
        tile[tile_h - img_h:, :img_w] = image
        write_cell(pixels, height, slot_idx % cols, slot_idx // cols, cell_w, cell_h, pad, tile)
        slot.stamp = stamp
        changed = True

    if not changed:
        return None

    atlas.pixels.foreach_set(pixels.ravel())
    atlas.update()
    # Pack the atlas to keep it in the .blend file
    atlas.pack()
    return warnings


def repad_tile_folder(mat_data, new_pad):
    """Rebuilds the atlas with new padding. Moves every cell, including missing files, and the UVs that use them."""
    material = get_folder_material(mat_data)
    atlas = sprytile_utils.get_material_texture(material) if material else None
    if atlas is None or len(mat_data.grids) == 0:
        return False
    tile_w, tile_h = mat_data.grids[0].grid
    old_pad = mat_data.atlas_padding
    cols, rows = mat_data.atlas_columns, mat_data.atlas_rows
    old_cell = (tile_w + old_pad * 2, tile_h + old_pad * 2)
    new_cell = (tile_w + new_pad * 2, tile_h + new_pad * 2)
    old_size = (cols * old_cell[0], rows * old_cell[1])
    new_size = (cols * new_cell[0], rows * new_cell[1])
    if tuple(atlas.size) != old_size:
        return False

    old_pixels = numpy.empty(old_size[0] * old_size[1] * 4, dtype=numpy.float32)
    atlas.pixels.foreach_get(old_pixels)
    old_pixels = old_pixels.reshape(old_size[1], old_size[0], 4)
    pixels = numpy.zeros((new_size[1], new_size[0], 4), dtype=numpy.float32)
    for row in range(rows):
        for col in range(cols):
            x0 = col * old_cell[0] + old_pad
            y_top = old_size[1] - row * old_cell[1] - old_pad
            tile = old_pixels[y_top - tile_h:y_top, x0:x0 + tile_w]
            write_cell(pixels, new_size[1], col, row, new_cell[0], new_cell[1], new_pad, tile)

    remap_padding(material, tile_w, tile_h, old_pad, new_pad, old_size, new_size)
    atlas.scale(*new_size)
    atlas.pixels.foreach_set(pixels.ravel())
    atlas.update()
    atlas.pack()
    mat_data.atlas_padding = new_pad
    set_grid_padding(mat_data, tile_w, tile_h)
    return True


def tag_redraw_all():
    wm = bpy.context.window_manager
    if wm is None:
        return
    for window in wm.windows:
        for area in window.screen.areas:
            if area.type in {'VIEW_3D', 'IMAGE_EDITOR'}:
                area.tag_redraw()


def watch_tile_folders():
    changed = False
    for scene in bpy.data.scenes:
        mat_list = getattr(scene, "sprytile_mats", None)
        if mat_list is None:
            continue
        for mat_data in mat_list:
            if not mat_data.tile_dir:
                continue
            try:
                result = sync_tile_folder(mat_data)
            except Exception as err:
                print("Sprytile: tile folder sync failed for %s: %s" % (mat_data.mat_id, err))
                continue
            if result is not None:
                changed = True
                for warning in result:
                    print("Sprytile:", warning)
    if changed:
        tag_redraw_all()
    return WATCH_INTERVAL


class UTIL_OP_SprytileLinkTileFolder(bpy.types.Operator):
    bl_idname = "sprytile.tile_folder_link"
    bl_label = "Add Tile Folder"
    bl_description = "Make a material from a folder of tile images. Changes to the folder update the tileset"
    bl_options = {'REGISTER', 'UNDO'}

    directory: bpy.props.StringProperty(subtype='DIR_PATH')
    filter_folder: bpy.props.BoolProperty(default=True, options={'HIDDEN'})
    filter_image: bpy.props.BoolProperty(default=True, options={'HIDDEN'})
    atlas_columns: bpy.props.IntProperty(
        name="Columns",
        description="Tiles in each atlas row. Do not change after you paint tiles",
        default=8,
        min=1,
        max=64
    )
    atlas_padding: bpy.props.IntProperty(
        name="Padding",
        description="Edge pixels repeated around each tile, so faces do not show lines from the next tile",
        default=DEFAULT_PADDING,
        min=0,
        max=8
    )

    @classmethod
    def poll(cls, context):
        return context.object is not None and context.object.type == 'MESH'

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        obj = context.object
        folder = abspath(self.directory)
        files = list_tile_files(folder)
        if not files:
            self.report({'ERROR'}, "No images found in %s" % folder)
            return {'CANCELLED'}

        tile_w, tile_h, _ = read_image_pixels(os.path.join(folder, files[0]))

        material = bpy.data.materials.new(name=os.path.basename(os.path.normpath(folder)))
        obj.data.materials.append(material)
        obj.active_material_index = len(obj.material_slots) - 1
        bpy.ops.sprytile.material_setup()

        rows = max(math.ceil(len(files) / self.atlas_columns), self.atlas_columns)
        pad = self.atlas_padding
        get_or_create_atlas(material, self.atlas_columns * (tile_w + pad * 2), rows * (tile_h + pad * 2))

        bpy.ops.sprytile.texture_setup('INVOKE_DEFAULT')
        bpy.ops.sprytile.validate_grids('INVOKE_DEFAULT')

        mat_data = sprytile_utils.get_mat_data(context, material.name)
        if mat_data is None:
            self.report({'ERROR'}, "Could not set up Sprytile data for " + material.name)
            return {'CANCELLED'}
        mat_data.tile_dir = bpy.path.relpath(folder) if bpy.data.filepath else folder
        mat_data.atlas_columns = self.atlas_columns
        mat_data.atlas_rows = rows
        mat_data.atlas_padding = pad
        set_grid_padding(mat_data, tile_w, tile_h)
        grid = mat_data.grids[0]
        grid.margin = (0, 0, 0, 0)
        grid.offset = (0, 0)
        grid.tile_selection = (0, rows - 1, 1, 1)

        for warning in sync_tile_folder(mat_data, force=True) or []:
            self.report({'WARNING'}, warning)

        obj.sprytile_gridid = grid.id
        bpy.ops.sprytile.build_grid_list()
        return {'FINISHED'}


class UTIL_OP_SprytileSyncTileFolder(bpy.types.Operator):
    bl_idname = "sprytile.tile_folder_sync"
    bl_label = "Reload Tile Folder"
    bl_description = "Copy all files from the tile folder into the atlas again"

    @classmethod
    def poll(cls, context):
        return context.object is not None and sprytile_utils.get_selected_grid(context) is not None

    def execute(self, context):
        grid = sprytile_utils.get_selected_grid(context)
        mat_data = sprytile_utils.get_mat_data(context, grid.mat_id)
        if mat_data is None or not mat_data.tile_dir:
            return {'CANCELLED'}
        if not os.path.isdir(abspath(mat_data.tile_dir)):
            self.report({'ERROR'}, "Tile folder not found: " + mat_data.tile_dir)
            return {'CANCELLED'}
        for warning in sync_tile_folder(mat_data, force=True) or []:
            self.report({'WARNING'}, warning)
        tag_redraw_all()
        return {'FINISHED'}


class UTIL_OP_SprytileRepadTileFolder(bpy.types.Operator):
    bl_idname = "sprytile.tile_folder_repad"
    bl_label = "Atlas Padding"
    bl_description = "Change the edge pixels around each tile. Moves the UVs that use the atlas"
    bl_options = {'REGISTER', 'UNDO'}

    padding: bpy.props.IntProperty(
        name="Padding",
        description="Edge pixels repeated around each tile, so faces do not show lines from the next tile",
        default=DEFAULT_PADDING,
        min=0,
        max=8
    )

    @classmethod
    def poll(cls, context):
        return get_selected_folder_data(context) is not None

    def invoke(self, context, event):
        mat_data = get_selected_folder_data(context)
        if mat_data.atlas_padding != 0:
            self.padding = mat_data.atlas_padding
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        mat_data = get_selected_folder_data(context)
        if mat_data is None or self.padding == mat_data.atlas_padding:
            return {'CANCELLED'}
        if not repad_tile_folder(mat_data, self.padding):
            self.report({'ERROR'}, "The atlas size does not match its tile folder settings")
            return {'CANCELLED'}
        tag_redraw_all()
        return {'FINISHED'}


def get_selected_folder_data(context):
    """Returns the material data of the selected grid, if it has a tile folder"""
    if context.object is None:
        return None
    grid = sprytile_utils.get_selected_grid(context)
    if grid is None:
        return None
    mat_data = sprytile_utils.get_mat_data(context, grid.mat_id)
    if mat_data is None or not mat_data.tile_dir:
        return None
    return mat_data


def get_missing_slots(mat_data):
    folder = abspath(mat_data.tile_dir)
    return [slot for slot in mat_data.tile_slots if not os.path.isfile(os.path.join(folder, slot.name))]


# Keep a reference to dynamic enum strings, or Blender can show bad text
_enum_cache = {}


def missing_slot_items(self, context):
    mat_data = get_selected_folder_data(context)
    items = []
    if mat_data is not None:
        cols = mat_data.atlas_columns
        for slot in get_missing_slots(mat_data):
            idx = mat_data.tile_slots.find(slot.name)
            items.append((slot.name, slot.name, "Cell %d (row %d, column %d)" % (idx, idx // cols + 1, idx % cols + 1)))
    _enum_cache["missing"] = items
    return items


def tile_file_items(self, context):
    mat_data = get_selected_folder_data(context)
    items = []
    if mat_data is not None:
        for name in list_tile_files(abspath(mat_data.tile_dir)):
            items.append((name, name, ""))
    _enum_cache["files"] = items
    return items


class UTIL_OP_SprytileReassignTile(bpy.types.Operator):
    bl_idname = "sprytile.tile_folder_reassign"
    bl_label = "Reassign Tile"
    bl_description = "Give the slot of a missing file to a different file"
    bl_options = {'REGISTER', 'UNDO'}

    missing_slot: bpy.props.EnumProperty(
        name="Missing",
        description="Slot of a file that is not in the folder",
        items=missing_slot_items
    )
    tile_file: bpy.props.EnumProperty(
        name="New File",
        description="File to put in the slot",
        items=tile_file_items
    )

    @classmethod
    def poll(cls, context):
        mat_data = get_selected_folder_data(context)
        return mat_data is not None and len(get_missing_slots(mat_data)) > 0

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=350)

    def execute(self, context):
        mat_data = get_selected_folder_data(context)
        if mat_data is None or not self.missing_slot or not self.tile_file:
            return {'CANCELLED'}
        slots = mat_data.tile_slots
        target = slots[self.missing_slot]
        old_name = target.name

        # Swap the names. Do not remove a slot, because then other slots move.
        file_idx = slots.find(self.tile_file)
        if file_idx >= 0:
            slots[file_idx].name = "\0swap"
            target.name = self.tile_file
            slots[file_idx].name = old_name
        else:
            target.name = self.tile_file
        target.stamp = ""

        for warning in sync_tile_folder(mat_data) or []:
            self.report({'WARNING'}, warning)
        tag_redraw_all()
        self.report({'INFO'}, "%s now fills the cell of %s" % (self.tile_file, old_name))
        return {'FINISHED'}


def draw_tile_folder_info(layout, context):
    """Shows the tile folder of the selected grid, if it has one"""
    grid = sprytile_utils.get_selected_grid(context)
    if grid is None:
        return
    mat_data = sprytile_utils.get_mat_data(context, grid.mat_id)
    if mat_data is None or not mat_data.tile_dir:
        return
    box = layout.box()
    row = box.row(align=True)
    row.prop(mat_data, "tile_dir", text="", icon='FILE_FOLDER')
    row.operator("sprytile.tile_folder_sync", text="", icon='FILE_REFRESH')
    if not os.path.isdir(abspath(mat_data.tile_dir)):
        box.label(text="Folder not found", icon='ERROR')
    else:
        row = box.row()
        row.label(text="%d tiles, %d slots" % (len(mat_data.tile_slots), mat_data.atlas_columns * mat_data.atlas_rows))
        row.operator("sprytile.tile_folder_repad", text="Padding: %d px" % mat_data.atlas_padding)
        missing = len(get_missing_slots(mat_data))
        if missing:
            row = box.row()
            row.label(text="%d missing file%s" % (missing, "" if missing == 1 else "s"), icon='ERROR')
            row.operator("sprytile.tile_folder_reassign", text="Reassign", icon='FILE_REFRESH')


classes = (
    UTIL_OP_SprytileLinkTileFolder,
    UTIL_OP_SprytileSyncTileFolder,
    UTIL_OP_SprytileRepadTileFolder,
    UTIL_OP_SprytileReassignTile,
)


def register():
    for cl in classes:
        bpy.utils.register_class(cl)
    if not bpy.app.timers.is_registered(watch_tile_folders):
        bpy.app.timers.register(watch_tile_folders, first_interval=WATCH_INTERVAL, persistent=True)


def unregister():
    if bpy.app.timers.is_registered(watch_tile_folders):
        bpy.app.timers.unregister(watch_tile_folders)
    for cl in classes:
        bpy.utils.unregister_class(cl)
