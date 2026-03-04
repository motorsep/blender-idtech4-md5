# ##### BEGIN GPL LICENSE BLOCK #####
#
#  This program is free software; you can redistribute it and/or
#  modify it under the terms of the GNU General Public License
#  as published by the Free Software Foundation; either version 2
#  of the License, or (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with this program; if not, write to the Free Software Foundation,
#  Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301, USA.
#
# ##### END GPL LICENSE BLOCK #####

# idTech 4 MD5 Exporter - Blender 4.x Port
#
# Original: Paul Zirkle (Keless), credit to der_ton
# v1.1.0 Gert De Roost: bone export filtering and reparenting
# v1.0.6 CodeManX: fixes and UI
# v2.0.0 Ported to Blender 4.2+:
#   All hand-rolled matrix math replaced with Blender built-ins:
#     matrix_invert()  -> .inverted()
#     point_by_matrix()-> matrix @ Vector
#     .col[3][0:3]     -> .translation
#     matrix2quaternion-> .to_quaternion()
#   * -> @ for matrix/vector multiplication
#   tessfaces -> loop_triangles + uv_layers
#   bone.Export -> bone collection membership (fallback: all bones)
#   register_module -> register_class
#   INFO_MT_file_export -> TOPBAR_MT_file_export

bl_info = {
    "name": "Export idTech4 MD5 (.md5mesh/.md5anim)",
    "author": "Paul Zirkle, der_ton, Gert De Roost, CodeManX, motorsep",
    "version": (2, 0, 0),
    "blender": (4, 2, 0),
    "location": "File > Export > idTech 4 MD5",
    "description": "Export idTech4 MD5 mesh and animation",
    "warning": "",
    "wiki_url": "",
    "tracker_url": "",
    "category": "Import-Export",
}

import bpy
import math
import os
import mathutils

from bpy.props import (
    BoolProperty,
    CollectionProperty,
    EnumProperty,
    FloatProperty,
    IntProperty,
    StringProperty,
)
from bpy_extras.io_utils import ExportHelper
from bpy.app.handlers import persistent

scale = 1.0
BONES = {}

@persistent
def fakeuser_for_actions(scene):
    for action in bpy.data.actions:
        action.use_fake_user = True


# ---------------------------------------------------------------------------
# MD5 Data Classes
# ---------------------------------------------------------------------------

class Material:
    def __init__(self, name):
        self.name = name
    def to_md5mesh(self):
        return self.name


class Mesh:
    def __init__(self, name):
        self.name = name
        self.submeshes = []
        self.next_submesh_id = 0
    def to_md5mesh(self):
        buf = ""
        for sm in self.submeshes:
            buf += "mesh {\n" + sm.to_md5mesh() + "}\n\n"
        return buf


class SubMesh:
    def __init__(self, mesh, material):
        self.material = material
        self.vertices = []
        self.faces = []
        self.weights = []
        self.next_vertex_id = 0
        self.next_weight_id = 0
        self.mesh = mesh
        self.name = mesh.name
        self.id = mesh.next_submesh_id
        mesh.next_submesh_id += 1
        mesh.submeshes.append(self)

    def bindtomesh(self, mesh):
        self.mesh.submeshes.remove(self)
        self.mesh = mesh
        self.id = mesh.next_submesh_id
        mesh.next_submesh_id += 1
        mesh.submeshes.append(self)

    def generateweights(self):
        self.weights = []
        self.next_weight_id = 0
        for vert in self.vertices:
            vert.generateweights()

    def to_md5mesh(self):
        self.generateweights()
        buf = "\tshader \"%s\"\n\n" % self.material.to_md5mesh()
        if not self.weights:
            return buf + "\tnumverts 0\n\n\tnumtris 0\n\n\tnumweights 0\n"
        buf += "\tnumverts %i\n" % len(self.vertices)
        for i, v in enumerate(self.vertices):
            buf += "\tvert %i %s\n" % (i, v.to_md5mesh())
        buf += "\n\tnumtris %i\n" % len(self.faces)
        for i, f in enumerate(self.faces):
            buf += "\ttri %i %s\n" % (i, f.to_md5mesh())
        buf += "\n\tnumweights %i\n" % len(self.weights)
        for i, w in enumerate(self.weights):
            buf += "\tweight %i %s\n" % (i, w.to_md5mesh())
        return buf


class Vertex:
    def __init__(self, submesh, loc, normal):
        self.loc = loc
        self.normal = normal
        self.maps = []
        self.influences = []
        self.weights = []
        self.firstweightindx = 0
        self.cloned_from = None
        self.clones = []
        self.submesh = submesh
        self.id = submesh.next_vertex_id
        submesh.next_vertex_id += 1
        submesh.vertices.append(self)

    def generateweights(self):
        self.firstweightindx = self.submesh.next_weight_id
        # MD5 format: max 4 bone influences per vertex
        if len(self.influences) > 4:
            self.influences.sort(key=lambda inf: inf.weight, reverse=True)
            self.influences = self.influences[:4]
        total = sum(inf.weight for inf in self.influences)
        if total != 0:
            for inf in self.influences:
                inf.weight /= total
        for inf in self.influences:
            idx = self.submesh.next_weight_id
            self.submesh.next_weight_id += 1
            w = Weight(inf.bone, inf.weight, self, idx,
                       self.loc[0], self.loc[1], self.loc[2])
            self.submesh.weights.append(w)
            self.weights.append(w)

    def to_md5mesh(self):
        if self.maps:
            buf = self.maps[0].to_md5mesh()
        else:
            buf = "( %f %f )" % (self.loc[0], self.loc[1])
        return buf + " %i %i" % (self.firstweightindx, len(self.influences))


class Map:
    def __init__(self, u, v):
        self.u = u
        self.v = v
    def to_md5mesh(self):
        return "( %f %f )" % (self.u, self.v)


class Weight:
    def __init__(self, bone, weight, vertex, weightindx, x, y, z):
        self.bone = bone
        self.weight = weight
        self.vertex = vertex
        self.indx = weightindx
        # Blender 4.x: use built-in .inverted() and @ operator
        # Replaces hand-rolled matrix_invert() + point_by_matrix()
        invbonematrix = bone.matrix.inverted()
        result = invbonematrix @ mathutils.Vector((x, y, z, 1.0))
        self.x, self.y, self.z = result[0], result[1], result[2]

    def to_md5mesh(self):
        global scale
        return "%i %f ( %f %f %f )" % (
            self.bone.id, self.weight,
            self.x * scale, self.y * scale, self.z * scale)


class Influence:
    def __init__(self, bone, weight):
        self.bone = bone
        self.weight = weight


class Face:
    def __init__(self, submesh, v1, v2, v3):
        self.vertex1 = v1
        self.vertex2 = v2
        self.vertex3 = v3
        self.submesh = submesh
        submesh.faces.append(self)
    def to_md5mesh(self):
        return "%i %i %i" % (self.vertex1.id, self.vertex3.id, self.vertex2.id)


class Skeleton:
    def __init__(self, MD5Version=10, commandline=""):
        self.bones = []
        self.MD5Version = MD5Version
        self.commandline = commandline
        self.next_bone_id = 0

    def to_md5mesh(self, numsubmeshes):
        buf = "MD5Version %i\n" % self.MD5Version
        buf += "commandline \"%s\"\n\n" % self.commandline
        buf += "numJoints %i\n" % self.next_bone_id
        buf += "numMeshes %i\n\n" % numsubmeshes
        buf += "joints {\n"
        for bone in self.bones:
            buf += bone.to_md5mesh()
        buf += "}\n\n"
        return buf


class Bone:
    def __init__(self, skeleton, parent, name, mat, theboneobj):
        self.parent = parent
        self.name = name
        self.children = []
        self.theboneobj = theboneobj
        self.is_animated = 0
        self.matrix = mat
        if parent:
            parent.children.append(self)
        self.skeleton = skeleton
        self.id = skeleton.next_bone_id
        skeleton.next_bone_id += 1
        skeleton.bones.append(self)
        BONES[name] = self

    def to_md5mesh(self):
        global scale
        buf = "\t\"%s\"\t" % self.name
        parentindex = self.parent.id if self.parent else -1
        buf += "%i " % parentindex
        # Blender 4.x: .translation replaces .col[3][0:3]
        pos = self.matrix.translation
        buf += "( %f %f %f ) " % (pos[0] * scale, pos[1] * scale, pos[2] * scale)
        # Blender 4.x: .to_quaternion() replaces hand-rolled matrix2quaternion
        bquat = self.matrix.to_quaternion()
        bquat.normalize()
        qx, qy, qz = bquat.x, bquat.y, bquat.z
        if bquat.w > 0:
            qx, qy, qz = -qx, -qy, -qz
        buf += "( %f %f %f )\t\t// " % (qx, qy, qz)
        if self.parent:
            buf += self.parent.name
        buf += "\n"
        return buf

class MD5Animation:
    def __init__(self, md5skel, MD5Version=10, commandline=""):
        self.framedata = []
        self.bounds = []
        self.baseframe = []
        self.skeleton = md5skel
        self.boneflags = []
        self.boneframedataindex = []
        self.MD5Version = MD5Version
        self.commandline = commandline
        self.numanimatedcomponents = 0
        self.framerate = bpy.data.scenes[0].render.fps
        self.numframes = 0
        for b in self.skeleton.bones:
            self.framedata.append([])
            self.baseframe.append([])
            self.boneflags.append(0)
            self.boneframedataindex.append(0)

    def to_md5anim(self):
        global scale
        currentframedataindex = 0
        for bone in self.skeleton.bones:
            if len(self.framedata[bone.id]) > 0:
                if len(self.framedata[bone.id]) > self.numframes:
                    self.numframes = len(self.framedata[bone.id])
                (x, y, z), (qw, qx, qy, qz) = self.framedata[bone.id][0]
                self.baseframe[bone.id] = (
                    x * scale, y * scale, z * scale, -qx, -qy, -qz)
                self.boneframedataindex[bone.id] = currentframedataindex
                self.boneflags[bone.id] = 63
                currentframedataindex += 6
                self.numanimatedcomponents = currentframedataindex
            else:
                rot = bone.matrix.to_quaternion()
                rot.normalize()
                # Blender 4.x: .translation replaces .col[3][0:3]
                tx, ty, tz = bone.matrix.translation
                self.baseframe[bone.id] = (
                    tx * scale, ty * scale, tz * scale,
                    -rot.x, -rot.y, -rot.z)

        buf = "MD5Version %i\n" % self.MD5Version
        buf += "commandline \"%s\"\n\n" % self.commandline
        buf += "numFrames %i\n" % self.numframes
        buf += "numJoints %i\n" % len(self.skeleton.bones)
        buf += "frameRate %i\n" % self.framerate
        buf += "numAnimatedComponents %i\n\n" % self.numanimatedcomponents

        buf += "hierarchy {\n"
        for bone in self.skeleton.bones:
            parentindex = bone.parent.id if bone.parent else -1
            flags = self.boneflags[bone.id]
            fdi = self.boneframedataindex[bone.id]
            buf += "\t\"%s\"\t%i %i %i\t//" % (bone.name, parentindex, flags, fdi)
            if bone.parent:
                buf += " " + bone.parent.name
            buf += "\n"
        buf += "}\n\n"

        buf += "bounds {\n"
        for b in self.bounds:
            buf += "\t( %f %f %f ) ( %f %f %f )\n" % b
        buf += "}\n\n"

        buf += "baseframe {\n"
        for b in self.baseframe:
            buf += "\t( %f %f %f ) ( %f %f %f )\n" % b
        buf += "}\n\n"

        for f in range(self.numframes):
            buf += "frame %i {\n" % f
            for b in self.skeleton.bones:
                if len(self.framedata[b.id]) > 0:
                    (x, y, z), (qw, qx, qy, qz) = self.framedata[b.id][f]
                    if qw > 0:
                        qx, qy, qz = -qx, -qy, -qz
                    buf += "\t%f %f %f %f %f %f\n" % (
                        x * scale, y * scale, z * scale, qx, qy, qz)
            buf += "}\n\n"
        return buf

    def addkeyforbone(self, boneid, time, loc, rot):
        self.framedata[boneid].append((loc, rot))


# ---------------------------------------------------------------------------
# Bounding box helpers
# ---------------------------------------------------------------------------

def getminmax(pts):
    if not pts:
        return ([0, 0, 0], [0, 0, 0])
    mn = [pts[0][0], pts[0][1], pts[0][2]]
    mx = [pts[0][0], pts[0][1], pts[0][2]]
    for p in pts[1:]:
        for j in range(3):
            if p[j] < mn[j]: mn[j] = p[j]
            if p[j] > mx[j]: mx[j] = p[j]
    return (mn, mx)


def generateboundingbox(objects, md5animation, framerange):
    global scale
    scn = bpy.context.scene
    for i in range(framerange[0], framerange[1] + 1):
        corners = []
        scn.frame_set(i)
        for obj in objects:
            if obj and obj.type == 'MESH' and len(obj.data.polygons) > 0:
                for v in obj.bound_box:
                    # Blender 4.x: @ replaces *
                    corners.append(obj.matrix_world @ mathutils.Vector(v))
        mn, mx = getminmax(corners)
        md5animation.bounds.append((
            mn[0] * scale, mn[1] * scale, mn[2] * scale,
            mx[0] * scale, mx[1] * scale, mx[2] * scale))


# ---------------------------------------------------------------------------
# Bone collection helpers
# ---------------------------------------------------------------------------

def get_md5_bc_name():
    return getattr(bpy.context.scene, 'md5_bone_collection', 'MD5_Bone_Collection')

def get_export_bone_names(armature_obj):
    """Bone collection if exists, otherwise ALL bones."""
    arm = armature_obj.data
    bc_name = get_md5_bc_name()
    if bc_name:
        try:
            bcol = arm.collections[bc_name]
            if bcol.bones and len(bcol.bones) > 0:
                names = set(b.name for b in bcol.bones)
                print("MD5 Export: bone collection '%s' (%d bones)" % (bc_name, len(names)))
                return names
        except (KeyError, IndexError):
            pass
    names = set(b.name for b in arm.bones)
    print("MD5 Export: no bone collection, exporting ALL %d bones" % len(names))
    return names

# ---------------------------------------------------------------------------
# Main export function
# ---------------------------------------------------------------------------

def save_md5(settings):
    print("Exporting selected objects...")
    bpy.ops.object.mode_set(mode='OBJECT')

    global BONES, scale
    scale = settings.scale
    thearmature = None

    skeleton = Skeleton(10, "Exported from Blender by io_export_md5.py")
    bpy.context.scene.frame_set(bpy.context.scene.frame_start)
    BONES = {}

    # --- First pass: skeleton ---
    for obj in bpy.context.selected_objects:
        if obj.type == 'ARMATURE':
            thearmature = obj
            w_matrix = obj.matrix_world
            export_names = get_export_bone_names(thearmature)

            def treat_bone(b, parent=None):
                if parent and b.parent and b.parent.name != parent.name:
                    return
                # Blender 4.x: @ replaces *
                mat = w_matrix @ b.matrix_local
                bone = Bone(skeleton, parent, b.name, mat, b)
                if b.children:
                    for child in b.children:
                        if child.name in export_names:
                            treat_bone(child, bone)

            for b in thearmature.data.bones:
                if not b.parent and b.name in export_names:
                    print("root bone: " + b.name)
                    treat_bone(b)
            break
    else:
        print("No armature selected! Quitting...")
        return

    # --- Second pass: meshes ---
    meshes = []
    for obj in bpy.context.selected_objects:
        if obj.type == 'MESH' and len(obj.data.vertices) > 0:
            me = obj.data
            mesh = Mesh(obj.name)
            print("Processing mesh: " + obj.name)
            meshes.append(mesh)

            w_matrix = obj.matrix_world
            verts = me.vertices

            # Blender 4.x: loop_triangles replaces tessfaces
            me.calc_loop_triangles()
            uv_layer = me.uv_layers.active
            tri_faces = list(me.loop_triangles)

            createA = createB = createC = 0

            while tri_faces:
                mat_idx = tri_faces[0].material_index
                try:
                    mat_name = me.materials[mat_idx].name
                except (IndexError, AttributeError):
                    mat_name = "no_material"

                material = Material(mat_name)
                submesh = SubMesh(mesh, material)
                vert_dict = {}

                for tri in tri_faces[:]:
                    tv = tri.vertices
                    # Remove degenerate
                    if len(tv) < 3 or tv[0] == tv[1] or tv[0] == tv[2] or tv[1] == tv[2]:
                        tri_faces.remove(tri)
                        continue
                    # Skip different material
                    if tri.material_index != mat_idx:
                        continue
                    tri_faces.remove(tri)

                    # Flat normal for non-smooth faces
                    if not tri.use_smooth:
                        p1 = verts[tv[0]].co
                        p2 = verts[tv[1]].co
                        p3 = verts[tv[2]].co
                        # Blender 4.x: @ replaces *
                        normal = (w_matrix.to_3x3() @ (p3 - p2).cross(p1 - p2)).normalized()

                    face_vertices = []
                    for i in range(3):
                        vi = tv[i]
                        vertex = vert_dict.get(vi, False)

                        if not vertex:
                            # Blender 4.x: @ replaces *
                            coord = w_matrix @ verts[vi].co
                            if tri.use_smooth:
                                normal = (w_matrix.to_3x3() @ verts[vi].normal).normalized()
                            vertex = vert_dict[vi] = Vertex(submesh, coord, normal)
                            createA += 1

                            # Gather bone influences
                            for g in me.vertices[vi].groups:
                                try:
                                    bone_name = obj.vertex_groups[g.group].name
                                    vertex.influences.append(
                                        Influence(BONES[bone_name], g.weight))
                                except (IndexError, KeyError):
                                    continue

                        elif not tri.use_smooth:
                            # Clone for flat shading
                            old_vertex = vertex
                            vertex = Vertex(submesh, vertex.loc, normal)
                            createB += 1
                            vertex.cloned_from = old_vertex
                            vertex.influences = old_vertex.influences
                            old_vertex.clones.append(vertex)

                        # UV handling
                        if uv_layer:
                            loop_idx = tri.loops[i]
                            uv = [uv_layer.data[loop_idx].uv[0],
                                  1.0 - uv_layer.data[loop_idx].uv[1]]
                            if not vertex.maps:
                                vertex.maps.append(Map(*uv))
                            elif vertex.maps[0].u != uv[0] or vertex.maps[0].v != uv[1]:
                                # Clone for different UV
                                found = False
                                for clone in vertex.clones:
                                    if clone.maps and \
                                       clone.maps[0].u == uv[0] and \
                                       clone.maps[0].v == uv[1]:
                                        vertex = clone
                                        found = True
                                        break
                                if not found:
                                    old_vertex = vertex
                                    vertex = Vertex(submesh, vertex.loc, vertex.normal)
                                    createC += 1
                                    vertex.cloned_from = old_vertex
                                    vertex.influences = old_vertex.influences
                                    vertex.maps.append(Map(*uv))
                                    old_vertex.clones.append(vertex)

                        face_vertices.append(vertex)

                    Face(submesh, face_vertices[0], face_vertices[1], face_vertices[2])

            print("created verts: A=%d B=%d C=%d" % (createA, createB, createC))

    if not meshes:
        print("No meshes found!")
        return

    # --- Export animations ---
    if not thearmature.animation_data:
        thearmature.animation_data_create()

    orig_action = thearmature.animation_data.action

    for a in settings.md5actions:
        if not a.export_action and settings.sel_only:
            continue

        arm_action = bpy.data.actions.get(a.name)
        if not arm_action:
            continue

        if len(arm_action.pose_markers) < 2:
            frame_range = (int(arm_action.frame_range[0]),
                           int(arm_action.frame_range[1]))
        else:
            pm_frames = [pm.frame for pm in arm_action.pose_markers]
            frame_range = (min(pm_frames), max(pm_frames))

        rangestart, rangeend = frame_range
        thearmature.animation_data.action = arm_action
        animation = MD5Animation(skeleton)

        currenttime = rangestart
        while currenttime <= rangeend:
            bpy.context.scene.frame_set(currenttime)
            pose = thearmature.pose

            for bonename in thearmature.data.bones.keys():
                # Blender 4.x: .copy() replaces Matrix(m) constructor
                posebonemat = pose.bones[bonename].matrix.copy()
                try:
                    bone = BONES[bonename]
                except KeyError:
                    continue

                if bone.parent:
                    # Blender 4.x: .inverted() @ replaces invert() then *
                    parentposemat = pose.bones[bone.parent.name].matrix.inverted()
                    posebonemat = parentposemat @ posebonemat
                else:
                    posebonemat = thearmature.matrix_world @ posebonemat

                # Blender 4.x: .translation replaces .col[3][0:3]
                loc = list(posebonemat.translation)
                rot = posebonemat.to_quaternion()
                rot.normalize()
                rot = [rot.w, rot.x, rot.y, rot.z]
                animation.addkeyforbone(bone.id, currenttime, loc, rot)
            currenttime += 1

        # Build anim filename
        if settings.prefix:
            prefix_str = (settings.name + "_") if settings.name else \
                (os.path.splitext(os.path.split(settings.savepath)[1])[0] + "_")
        else:
            prefix_str = ""
        md5anim_filename = os.path.join(
            os.path.split(settings.savepath)[0],
            prefix_str + arm_action.name + ".md5anim")

        try:
            f = open(md5anim_filename, 'w')
        except IOError:
            print("IOError writing " + md5anim_filename)
            continue

        objects = []
        for submesh in meshes[0].submeshes:
            if len(submesh.weights) > 0:
                obj = None
                for sob in bpy.context.selected_objects:
                    if sob and sob.type == 'MESH' and sob.name == submesh.name:
                        obj = sob
                objects.append(obj)

        generateboundingbox(objects, animation, [rangestart, rangeend])
        f.write(animation.to_md5anim())
        f.close()
        print("saved anim to " + md5anim_filename)

    thearmature.animation_data.action = orig_action

    # --- Save mesh ---
    if len(meshes) > 1:
        for mi in range(1, len(meshes)):
            for submesh in meshes[mi].submeshes:
                submesh.bindtomesh(meshes[0])

    md5mesh_filename = settings.savepath
    if md5mesh_filename:
        try:
            f = open(md5mesh_filename, 'w')
        except IOError:
            print("IOError writing " + md5mesh_filename)
            return
        f.write(skeleton.to_md5mesh(len(meshes[0].submeshes)))
        f.write(meshes[0].to_md5mesh())
        f.close()
        print("saved mesh to " + md5mesh_filename)


class md5Settings:
    def __init__(self, savepath, scale, actions, sel_only, prefix, name):
        self.savepath = savepath
        self.scale = scale
        self.md5actions = actions
        self.sel_only = sel_only
        self.name = name
        self.prefix = prefix

# ---------------------------------------------------------------------------
# UI Classes
# ---------------------------------------------------------------------------

class ActionsPropertyGroup(bpy.types.PropertyGroup):
    export_action: BoolProperty(default=False, name="")


class MD5_UL_ActionsList(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon,
                  active_data, active_propname, index):
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            layout.prop(item, "export_action", text=item.name)
        elif self.layout_type in {'GRID'}:
            layout.alignment = 'CENTER'
            layout.prop(item, "export_action", text="")


class MD5_OT_SelectActions(bpy.types.Operator):
    """(De-)Select all actions or invert selection for export"""
    bl_idname = "export.md5_select_actions"
    bl_label = "Select actions"

    action: EnumProperty(
        items=(("SELECT", "Select all", ""),
               ("DESELECT", "Deselect all", ""),
               ("INVERT", "Invert selection", "")),
        default="SELECT")

    def execute(self, context):
        for a in context.active_operator.md5actions:
            if self.action == "DESELECT":
                a.export_action = False
            elif self.action == "INVERT":
                a.export_action = not a.export_action
            else:
                a.export_action = True
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Bone collection management panel + operators
# ---------------------------------------------------------------------------

class MD5_PT_BonePanel(bpy.types.Panel):
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "MD5"
    bl_label = "MD5 Export Setup"

    def draw(self, context):
        layout = self.layout
        box = layout.box()
        box.label(text="Bone Collection (optional):")
        box.prop(context.scene, "md5_bone_collection", text="")
        box.label(text="Leave empty to export all bones.", icon='INFO')

        obj = context.active_object
        if obj and obj.type == 'ARMATURE':
            bc_name = get_md5_bc_name()
            has_col = any(bc.name == bc_name for bc in obj.data.collections)
            if not has_col:
                layout.operator("export.md5_create_bone_collection")
            else:
                layout.label(text="Collection '%s' exists." % bc_name,
                             icon='CHECKMARK')
                if context.mode in {'POSE', 'EDIT_ARMATURE'}:
                    col = layout.column(align=True)
                    col.operator("export.md5_bones_add")
                    col.operator("export.md5_bones_remove")


class MD5_OT_CreateBoneCollection(bpy.types.Operator):
    """Create MD5 bone collection with all bones"""
    bl_idname = "export.md5_create_bone_collection"
    bl_label = "Create MD5 Bone Collection (All Bones)"

    def execute(self, context):
        obj = context.active_object
        if not obj or obj.type != 'ARMATURE':
            self.report({'ERROR'}, "Select an armature")
            return {'CANCELLED'}
        bc_name = get_md5_bc_name()
        try:
            bcol = obj.data.collections[bc_name]
        except KeyError:
            bcol = obj.data.collections.new(bc_name)
        for b in obj.data.bones:
            bcol.assign(b)
        self.report({'INFO'}, "Created '%s' with %d bones" % (
            bc_name, len(obj.data.bones)))
        return {'FINISHED'}


class MD5_OT_BonesAdd(bpy.types.Operator):
    """Add selected bones to MD5 bone collection"""
    bl_idname = "export.md5_bones_add"
    bl_label = "Add Selected to MD5"

    def execute(self, context):
        obj = context.active_object
        if not obj or obj.type != 'ARMATURE':
            return {'CANCELLED'}
        try:
            bcol = obj.data.collections[get_md5_bc_name()]
        except KeyError:
            return {'CANCELLED'}
        if context.mode == 'POSE':
            for pb in context.selected_pose_bones:
                bcol.assign(pb.bone)
        elif context.mode == 'EDIT_ARMATURE':
            for eb in context.selected_editable_bones:
                bcol.assign(eb)
        return {'FINISHED'}


class MD5_OT_BonesRemove(bpy.types.Operator):
    """Remove selected bones from MD5 bone collection"""
    bl_idname = "export.md5_bones_remove"
    bl_label = "Remove Selected from MD5"

    def execute(self, context):
        obj = context.active_object
        if not obj or obj.type != 'ARMATURE':
            return {'CANCELLED'}
        try:
            bcol = obj.data.collections[get_md5_bc_name()]
        except KeyError:
            return {'CANCELLED'}
        if context.mode == 'POSE':
            for pb in context.selected_pose_bones:
                bcol.unassign(pb.bone)
        elif context.mode == 'EDIT_ARMATURE':
            for eb in context.selected_editable_bones:
                bcol.unassign(eb)
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Export operator
# ---------------------------------------------------------------------------

class MD5_OT_Export(bpy.types.Operator, ExportHelper):
    """Export to idTech 4 MD5 (.md5mesh + .md5anim)"""
    bl_idname = "export.md5"
    bl_label = "Export MD5"
    filename_ext = ".md5mesh"

    filter_glob: StringProperty(
        default="*.md5mesh;*.md5anim",
        options={'HIDDEN'})

    md5name: StringProperty(
        name="MD5 Name",
        description="Anim file prefix (optional)",
        maxlen=64, default="")

    md5scale: FloatProperty(
        name="Scale", description="Scale all objects",
        default=1.0, precision=5)

    use_sel_only: BoolProperty(
        name="Only selected from list:", default=False)

    use_prefix: BoolProperty(
        name="Prefix with MD5name",
        description="Use MD5name as prefix for MD5anim files",
        default=False)

    md5actions_idx: IntProperty()

    def draw(self, context):
        layout = self.layout

        box = layout.box()
        box.prop(self, "md5name")
        sub = box.row()
        sub.enabled = len(self.md5name) == 0
        sub.label(text=os.path.splitext(os.path.basename(self.filepath))[0])
        box.prop(self, "md5scale")

        a_count = len(self.md5actions)
        if a_count == 0:
            a_count_str = "No animation data!"
        elif self.use_sel_only:
            a_count = len([a for a in self.md5actions if a.export_action])
            a_count_str = str(a_count)
        else:
            a_count_str = str(a_count) + " (all)"

        layout.label(text="Export actions: %s" % a_count_str)

        if a_count > 0 or self.use_sel_only:
            layout.prop(self, "use_sel_only")
            col = layout.column()
            col.active = self.use_sel_only
            col.template_list("MD5_UL_ActionsList", "",
                              self, "md5actions",
                              self, "md5actions_idx",
                              rows=min(len(self.md5actions), 8))
            sub = col.row(align=True)
            sub.operator("export.md5_select_actions",
                         text="Select").action = "SELECT"
            sub.operator("export.md5_select_actions",
                         text="Deselect").action = "DESELECT"
            sub.operator("export.md5_select_actions",
                         text="Invert").action = "INVERT"
            layout.prop(self, "use_prefix")

    def execute(self, context):
        settings = md5Settings(
            savepath=self.filepath,
            scale=self.md5scale,
            actions=self.md5actions,
            sel_only=self.use_sel_only,
            prefix=self.use_prefix,
            name=self.md5name)
        save_md5(settings)
        return {'FINISHED'}

    def invoke(self, context, event):
        actions = self.md5actions
        actions.clear()
        for action in bpy.data.actions:
            for fcurve in action.fcurves:
                if fcurve.data_path.startswith("pose.bones"):
                    break
            else:
                continue
            item = actions.add()
            item.name = action.name
        return super().invoke(context, event)


# ---------------------------------------------------------------------------
# Menu
# ---------------------------------------------------------------------------

def menu_func(self, context):
    self.layout.operator(MD5_OT_Export.bl_idname,
                         text="idTech 4 MD5 (.md5mesh/.md5anim)")


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

classes = (
    ActionsPropertyGroup,
    MD5_UL_ActionsList,
    MD5_OT_SelectActions,
    MD5_PT_BonePanel,
    MD5_OT_CreateBoneCollection,
    MD5_OT_BonesAdd,
    MD5_OT_BonesRemove,
    MD5_OT_Export,
)


def register():
    # Register PropertyGroup first
    bpy.utils.register_class(ActionsPropertyGroup)

    # Inject CollectionProperty before registering ExportMD5
    if 'md5actions' not in MD5_OT_Export.__annotations__:
        MD5_OT_Export.__annotations__['md5actions'] = CollectionProperty(
            type=ActionsPropertyGroup)

    # Register remaining classes
    for cls in classes:
        if cls is ActionsPropertyGroup:
            continue
        bpy.utils.register_class(cls)

    # Scene property for bone collection name
    bpy.types.Scene.md5_bone_collection = StringProperty(
        name="MD5 Bone Collection",
        description="Bone collection for MD5 export (leave empty to export all bones)",
        default="MD5_Bone_Collection")

    bpy.types.TOPBAR_MT_file_export.append(menu_func)
    bpy.app.handlers.save_pre.append(fakeuser_for_actions)


def unregister():
    bpy.app.handlers.save_pre.remove(fakeuser_for_actions)
    bpy.types.TOPBAR_MT_file_export.remove(menu_func)

    if hasattr(bpy.types.Scene, 'md5_bone_collection'):
        del bpy.types.Scene.md5_bone_collection

    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
