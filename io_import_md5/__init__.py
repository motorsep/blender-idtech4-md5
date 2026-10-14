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

# idTech 4 MD5 Importer - Blender 5.2+
#
# Companion to io_export_md5 and written against its conventions, so an
# imported model re-exports to an equivalent .md5mesh/.md5anim:
#   * joint orientation in the file is the xyz of the CONJUGATE of Blender's
#     quaternion, with w >= 0 implied (idQuat::ToMat3 rows are the axes,
#     Blender matrix columns are)
#   * triangles are written with reversed winding (a, c, b)
#   * texture V is flipped (1 - v)
#   * v12 normals are stored in the space of the vertex's dominant joint
#     (highest weight, first one on ties), as Model_md5.cpp reads them
#
# v1.0.0 initial version:
#   md5mesh v10/v12 -> armature + one mesh object per 'mesh' block
#   md5anim list (scanned from the mesh's folder) -> one Action per file,
#     or all of them in one Action with pose markers
#   import scale, rotation about the vertical axis, bone orientation fix

bl_info = {
    "name": "Import idTech4.x MD5 (.md5mesh/.md5anim)",
    "author": "motorsep/Claude",
    "version": (1, 0, 0),
    "blender": (5, 2, 0),
    "location": "File > Import > idTech 4 MD5",
    "description": "Import idTech4.x MD5 mesh (v10 and v12) and animations",
    "warning": "",
    "wiki_url": "",
    "tracker_url": "",
    "category": "Import-Export",
}

import bpy
import math
import os
import re

from mathutils import Matrix, Quaternion, Vector
from bpy.props import (
    BoolProperty,
    CollectionProperty,
    EnumProperty,
    FloatProperty,
    IntProperty,
    StringProperty,
)
from bpy_extras.io_utils import ImportHelper


# ---------------------------------------------------------------------------
# Tokenizer
# ---------------------------------------------------------------------------

class MD5ParseError(Exception):
    pass


# quoted string | // comment (dropped) | bracket | bare word or number
_TOKEN_RE = re.compile(r'"([^"]*)"|//[^\n]*|([(){}]|[^\s(){}"]+)')


class Tokens:
    def __init__(self, text, path):
        self.path = path
        toks = []
        for m in _TOKEN_RE.finditer(text):
            g = m.lastindex
            if g:
                toks.append(m.group(g))
        self.toks = toks
        self.pos = 0

    def error(self, msg):
        raise MD5ParseError("%s: %s (token %d)" % (
            os.path.basename(self.path), msg, self.pos))

    def peek(self):
        return self.toks[self.pos] if self.pos < len(self.toks) else None

    def next(self):
        if self.pos >= len(self.toks):
            self.error("unexpected end of file")
        t = self.toks[self.pos]
        self.pos += 1
        return t

    def expect(self, s):
        t = self.next()
        if t != s:
            self.error("expected '%s', found '%s'" % (s, t))

    def int(self):
        t = self.next()
        try:
            return int(t)
        except ValueError:
            self.error("expected an integer, found '%s'" % t)

    def float(self):
        t = self.next()
        try:
            return float(t)
        except ValueError:
            self.error("expected a number, found '%s'" % t)

    def vec(self, n):
        self.expect("(")
        v = [self.float() for _ in range(n)]
        self.expect(")")
        return v


def file_quat(x, y, z):
    """MD5 compressed joint quaternion -> Blender Quaternion.

    The engine rebuilds w = sqrt(|1 - x^2 - y^2 - z^2|) (idCQuat::ToQuat) and
    reads its matrix by rows; Blender reads by columns, so the Blender
    rotation is the conjugate."""
    w = math.sqrt(abs(1.0 - (x * x + y * y + z * z)))
    q = Quaternion((w, -x, -y, -z))
    q.normalize()
    return q


# ---------------------------------------------------------------------------
# File parsing
# ---------------------------------------------------------------------------

class MD5Joint:
    __slots__ = ("name", "parent", "pos", "quat")

    def __init__(self, name, parent, pos, quat):
        self.name = name
        self.parent = parent
        self.pos = pos
        self.quat = quat


class MD5MeshBlock:
    def __init__(self):
        self.shader = ""
        self.verts = []    # (u, v, first_weight, num_weights, normal or None)
        self.tris = []     # (a, b, c) as in the file
        self.weights = []  # (joint, bias, (x, y, z))
        self.colors = None # [(r, g, b, a)] per vert, v12 only


def _read_text(path, head_until=None):
    """Whole file, or (when head_until is a regex) only up to its first match.
    The head read keeps the folder scan cheap on big anim libraries."""
    if head_until is None:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    chunk = 65536
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        text = ""
        while True:
            part = f.read(chunk)
            text += part
            m = head_until.search(text)
            if m:
                return text[:m.start()]
            if not part:
                return text
            chunk *= 2


def _parse_joints(t, num_joints):
    joints = []
    t.expect("{")
    for i in range(num_joints):
        name = t.next()
        parent = t.int()
        if parent >= i:
            t.error("joint '%s' has parent %d, must come before it" % (name, parent))
        pos = Vector(t.vec(3))
        q = t.vec(3)
        joints.append(MD5Joint(name, parent, pos, file_quat(*q)))
    t.expect("}")
    return joints


def _parse_mesh_block(t):
    mb = MD5MeshBlock()
    t.expect("{")
    while True:
        key = t.next()
        if key == "}":
            break
        if key == "shader":
            mb.shader = t.next()
        elif key == "numverts":
            n = t.int()
            for _ in range(n):
                t.expect("vert")
                t.int()
                u, v = t.vec(2)
                first = t.int()
                count = t.int()
                normal = None
                if t.peek() == "(":  # v12: ( nx ny nz ) ( tx ty tz w )
                    normal = t.vec(3)
                    t.vec(4)
                mb.verts.append((u, v, first, count, normal))
        elif key == "numtris":
            n = t.int()
            for _ in range(n):
                t.expect("tri")
                t.int()
                mb.tris.append((t.int(), t.int(), t.int()))
        elif key == "numweights":
            n = t.int()
            for _ in range(n):
                t.expect("weight")
                t.int()
                joint = t.int()
                bias = t.float()
                mb.weights.append((joint, bias, tuple(t.vec(3))))
        elif key == "numvertexcolors":
            n = t.int()
            mb.colors = []
            for _ in range(n):
                t.expect("vertexcolor")
                t.int()
                mb.colors.append(tuple(t.vec(4)))
        else:
            t.error("unknown keyword '%s' in mesh block" % key)
    return mb


_MESH_HEAD_END = re.compile(r"^\s*mesh\s*\{", re.M)


def parse_md5mesh(path, joints_only=False):
    text = _read_text(path, _MESH_HEAD_END if joints_only else None)
    t = Tokens(text, path)
    version = 0
    num_joints = 0
    joints = []
    meshes = []
    while t.peek() is not None:
        key = t.next()
        if key == "MD5Version":
            version = t.int()
        elif key == "commandline":
            t.next()
        elif key == "numJoints":
            num_joints = t.int()
        elif key == "numMeshes":
            t.int()
        elif key == "joints":
            joints = _parse_joints(t, num_joints)
            if joints_only:
                break
        elif key == "mesh":
            meshes.append(_parse_mesh_block(t))
        else:
            t.error("unknown keyword '%s'" % key)
    if not joints:
        raise MD5ParseError("%s: no joints" % os.path.basename(path))
    return version, joints, meshes


class MD5Anim:
    def __init__(self):
        self.frame_rate = 24
        self.num_frames = 0
        self.num_components = 0
        self.joints = []  # (name, parent, flags, start)
        self.base = []    # (Vector pos, (qx, qy, qz) raw)
        self.frames = []  # list of float lists


_ANIM_HEAD_END = re.compile(r"^\s*bounds\s*\{", re.M)


def parse_md5anim(path, header_only=False):
    text = _read_text(path, _ANIM_HEAD_END if header_only else None)
    t = Tokens(text, path)
    a = MD5Anim()
    num_joints = 0
    while t.peek() is not None:
        key = t.next()
        if key == "MD5Version":
            t.int()
        elif key == "commandline":
            t.next()
        elif key == "numFrames":
            a.num_frames = t.int()
        elif key == "numJoints":
            num_joints = t.int()
        elif key == "frameRate":
            a.frame_rate = t.int()
        elif key == "numAnimatedComponents":
            a.num_components = t.int()
        elif key == "hierarchy":
            t.expect("{")
            for _ in range(num_joints):
                a.joints.append((t.next(), t.int(), t.int(), t.int()))
            t.expect("}")
            if header_only:
                break
        elif key == "bounds":
            t.expect("{")
            for _ in range(a.num_frames):
                t.vec(3)
                t.vec(3)
            t.expect("}")
        elif key == "baseframe":
            t.expect("{")
            for _ in range(num_joints):
                a.base.append((Vector(t.vec(3)), tuple(t.vec(3))))
            t.expect("}")
        elif key == "frame":
            t.int()
            t.expect("{")
            a.frames.append([t.float() for _ in range(a.num_components)])
            t.expect("}")
        else:
            t.error("unknown keyword '%s'" % key)
    if not header_only:
        if len(a.base) != len(a.joints):
            raise MD5ParseError("%s: baseframe has %d joints, hierarchy %d" % (
                os.path.basename(path), len(a.base), len(a.joints)))
        if len(a.frames) != a.num_frames:
            print("MD5 Import: WARNING %s declares %d frames, has %d" % (
                os.path.basename(path), a.num_frames, len(a.frames)))
            a.num_frames = len(a.frames)
    return a


# ---------------------------------------------------------------------------
# Rigid transforms as (Vector t, Quaternion q) pairs
# ---------------------------------------------------------------------------

def xf_mul(a, b):
    return (a[0] + a[1] @ b[0], a[1] @ b[1])


def xf_inv(a):
    qi = a[1].inverted()
    return (qi @ -a[0], qi)


def xf_from_matrix(m):
    q = m.to_quaternion()
    q.normalize()
    return (m.translation.copy(), q)


# ---------------------------------------------------------------------------
# Armature
# ---------------------------------------------------------------------------

def _relabel(axis):
    """Rotation that makes the bone's Y axis the joint's local axis
    `axis` = (index, sign), the smallest such turn (90 or 180 degrees)."""
    i, sign = axis
    if i == 1:
        return Matrix.Identity(3) if sign > 0 else Matrix.Rotation(math.pi, 3, 'Z')
    if i == 0:
        return Matrix.Rotation(-math.pi / 2 if sign > 0 else math.pi / 2, 3, 'Z')
    return Matrix.Rotation(math.pi / 2 if sign > 0 else -math.pi / 2, 3, 'X')


def _best_axis(rot, d):
    best = None
    best_dot = -2.0
    for i in range(3):
        dot = rot.col[i].dot(d)
        for sign in (1, -1):
            if dot * sign > best_dot:
                best_dot = dot * sign
                best = (i, sign)
    return best


def build_armature(context, name, joints, jt, jq, fix_orientation, deform):
    """Create the armature from model-space joint transforms (jt, jq).
    `deform` is the set of joint indexes that carry vertex weights.

    Returns (arm_obj, bone_names, rest, corr): the bone name of each joint
    (Blender may shorten or suffix it), each bone's actual rest transform
    read back from Blender, and the per-bone correction rotation
    C = joint^-1 * rest. C is identity (to float precision) without the
    orientation fix; animation import poses each bone at joint * C."""
    n = len(joints)
    children = [[] for _ in range(n)]
    subtree = [1] * n
    deforming = [1 if i in deform else 0 for i in range(n)]  # in subtree
    for i, j in enumerate(joints):
        if j.parent >= 0:
            children[j.parent].append(i)
    for i in range(n - 1, -1, -1):
        if joints[i].parent >= 0:
            subtree[joints[i].parent] += subtree[i]
            deforming[joints[i].parent] += deforming[i]

    lo = Vector(jt[0])
    hi = Vector(jt[0])
    for p in jt:
        for k in range(3):
            lo[k] = min(lo[k], p[k])
            hi[k] = max(hi[k], p[k])
    diag = (hi - lo).length
    if diag < 1e-6:
        diag = 1.0
    default_len = diag * 0.05
    min_len = diag * 0.005

    # The bone points at its "main" child: the one carrying the most
    # deforming joints, then the largest part of the hierarchy (the spine
    # rather than a leg for a pelvis), ties broken by distance. A child sitting
    # on the joint (Rhand -> Rhand1) is looked through to its own children.
    # A deforming bone never aims at a branch with no weights in it: weapon/
    # PDA attachers often sit at the model origin, far below the hand.
    eps = diag * 1e-4
    main_child = [None] * n
    for i in range(n):
        best = None
        stack = list(children[i])
        while stack:
            c = stack.pop()
            dist = (jt[c] - jt[i]).length
            if dist <= eps:
                stack.extend(children[c])
                continue
            if i in deform and deforming[c] == 0:
                continue
            k = (deforming[c], subtree[c], dist)
            if best is None or k > best[0]:
                best = (k, c)
        if best:
            main_child[i] = best[1]

    # Bones that deform the mesh are as long as the gap to their child.
    # Attachment/helper joints (origin, weapon and effect points) often have
    # a far-off child, which would make them huge, so bones without weights
    # are capped at half the typical deforming bone. Length is display only:
    # it moves the tail, never the joint transform.
    spans = sorted((jt[main_child[i]] - jt[i]).length for i in range(n)
                   if i in deform and main_child[i] is not None)
    helper_len = spans[len(spans) // 2] * 0.5 if spans else default_len

    arm_data = bpy.data.armatures.new(name)
    arm_obj = bpy.data.objects.new(name, arm_data)
    arm_obj.show_in_front = True
    context.collection.objects.link(arm_obj)
    context.view_layer.objects.active = arm_obj
    arm_obj.select_set(True)
    bpy.ops.object.mode_set(mode='EDIT')

    edit_bones = arm_data.edit_bones
    ebs = []
    axis_used = [None] * n
    lengths = [default_len] * n
    for i, j in enumerate(joints):
        rot = jq[i].to_matrix()
        c = main_child[i]
        if c is not None:
            d = jt[c] - jt[i]
            length = d.length
            d.normalize()
        else:
            d = None
            length = lengths[j.parent] * 0.5 if j.parent >= 0 else default_len
        if i not in deform:
            length = min(length, helper_len)
        lengths[i] = max(length, min_len)

        if fix_orientation:
            if d is None:
                # leaf: keep the local axis convention its parent pointed along
                axis = axis_used[j.parent] if j.parent >= 0 else (1, 1)
                rot = rot @ _relabel(axis)
            else:
                axis = _best_axis(rot, d)
                rot = rot @ _relabel(axis)
                rot = rot.col[1].rotation_difference(d).to_matrix() @ rot
            axis_used[i] = axis

        eb = edit_bones.new(j.name)
        eb.head = (0.0, 0.0, 0.0)
        eb.tail = (0.0, lengths[i], 0.0)
        m = rot.to_4x4()
        m.translation = jt[i]
        eb.matrix = m
        if j.parent >= 0:
            eb.parent = ebs[j.parent]
        ebs.append(eb)
    bone_names = [eb.name for eb in ebs]
    bpy.ops.object.mode_set(mode='OBJECT')

    rest = []
    corr = []
    for i, bname in enumerate(bone_names):
        b = arm_data.bones[bname]
        r = xf_from_matrix(b.matrix_local)
        rest.append(r)
        c = jq[i].inverted() @ r[1]
        c.normalize()
        corr.append(c)
        if fix_orientation:
            # lets a later tool recover the file's joint frame: joint = rest * C^-1
            b["md5_joint_correction"] = list(c)
    return arm_obj, bone_names, rest, corr


# ---------------------------------------------------------------------------
# Meshes
# ---------------------------------------------------------------------------

def _get_material(name):
    mat = bpy.data.materials.get(name)
    if mat is None:
        mat = bpy.data.materials.new(name)
    return mat


def build_mesh_object(context, mb, index, stem, joints, jt, jq, bone_names,
                      scale, arm_obj, merge, use_normals):
    nv = len(mb.verts)
    if nv == 0 or not mb.tris:
        return None

    # bind-pose position, aggregated weights and (v12) normal of every md5 vert
    pos = []
    infl = []
    nrm = []
    for (u, v, first, count, normal) in mb.verts:
        p = Vector((0.0, 0.0, 0.0))
        agg = {}
        best_j = -1
        best_w = 0.0
        for k in range(first, first + count):
            if k >= len(mb.weights):
                break
            ji, bias, off = mb.weights[k]
            p += (jt[ji] + jq[ji] @ (Vector(off) * scale)) * bias
            agg[ji] = agg.get(ji, 0.0) + bias
            if bias > best_w:
                best_w = bias
                best_j = ji
        pos.append(p)
        infl.append(agg)
        if normal is not None and best_j >= 0:
            nrm.append((jq[best_j] @ Vector(normal)).normalized())
        else:
            nrm.append(None)
    have_normals = use_normals and all(x is not None for x in nrm)

    # MD5 verts are split wherever UVs or (v12) normals differ. Weld the
    # ones that deform identically (same place, same weights) back together;
    # UVs and normals stay per corner, so nothing visible is lost.
    vmap = list(range(nv))
    out_src = list(range(nv))  # Blender vert -> a representative md5 vert
    if merge:
        eps = 1e-4 * max(abs(scale), 1e-6)
        keys = {}
        out_src = []
        for i in range(nv):
            p = pos[i]
            key = (round(p.x / eps), round(p.y / eps), round(p.z / eps),
                   tuple(sorted((j, round(w, 4)) for j, w in infl[i].items())))
            bi = keys.get(key)
            if bi is None:
                bi = keys[key] = len(out_src)
                out_src.append(i)
            vmap[i] = bi

    faces = []
    corners = []  # md5 vert per face corner
    seen = set()
    for (a, b, c) in mb.tris:
        if max(a, b, c) >= nv or min(a, b, c) < 0:
            continue
        fv = [vmap[a], vmap[c], vmap[b]]  # md5 winding is reversed
        if fv[0] == fv[1] or fv[1] == fv[2] or fv[0] == fv[2]:
            continue  # zero-area triangle (or welded into one)
        key = frozenset(fv)
        if key in seen:
            # same three verts as an earlier face (a back face sharing UVs):
            # give it its own verts, Blender keeps only one face per vert set
            fv = []
            for s in (a, c, b):
                fv.append(len(out_src))
                out_src.append(s)
        seen.add(frozenset(fv))
        faces.append(fv)
        corners.extend((a, c, b))

    shader = mb.shader or "no_material"
    base = shader.replace("\\", "/").rstrip("/").split("/")[-1] or "%s_mesh%d" % (stem, index)
    me = bpy.data.meshes.new(base)
    me.from_pydata([pos[s] for s in out_src], [], faces)

    uv_layer = me.uv_layers.new(name="UVMap")
    uvs = []
    for s in corners:
        uvs.append(mb.verts[s][0])
        uvs.append(1.0 - mb.verts[s][1])
    uv_layer.data.foreach_set("uv", uvs)

    if mb.colors and len(mb.colors) == nv:
        col = me.color_attributes.new("Col", 'FLOAT_COLOR', 'CORNER')
        flat = []
        for s in corners:
            flat.extend(mb.colors[s])
        col.data.foreach_set("color", flat)

    me.materials.append(_get_material(shader))
    me.polygons.foreach_set("use_smooth", [True] * len(faces))
    me.update()
    if have_normals:
        me.normals_split_custom_set([nrm[s] for s in corners])

    obj = bpy.data.objects.new(base, me)
    context.collection.objects.link(obj)
    obj.parent = arm_obj
    mod = obj.modifiers.new("Armature", 'ARMATURE')
    mod.object = arm_obj

    groups = {}
    batches = {}
    for bi, s in enumerate(out_src):
        for ji, w in infl[s].items():
            batches.setdefault((ji, w), []).append(bi)
    for (ji, w), verts in batches.items():
        vg = groups.get(ji)
        if vg is None:
            vg = groups[ji] = obj.vertex_groups.new(name=bone_names[ji])
        vg.add(verts, w, 'REPLACE')
    obj.select_set(True)
    return obj


# ---------------------------------------------------------------------------
# Animations
# ---------------------------------------------------------------------------

def anim_bone_keys(anim, joints, bone_rest, corr, scale, rz):
    """Per-bone (locations, quaternions) for every frame of `anim`, as pose
    bone basis values. Bones the anim does not drive stay at rest."""
    n = len(joints)
    a_index = {name: i for i, (name, _p, _f, _s) in enumerate(anim.joints)}
    src = [a_index.get(j.name) for j in joints]
    rel = []
    for i, j in enumerate(joints):
        if j.parent >= 0:
            rel.append(xf_mul(xf_inv(bone_rest[j.parent]), bone_rest[i]))
        else:
            rel.append(bone_rest[i])
    rel_inv = [xf_inv(r) for r in rel]

    locs = [[] for _ in range(n)]
    quats = [[] for _ in range(n)]
    na = len(anim.joints)
    for frame in anim.frames:
        mt = [None] * na
        mq = [None] * na
        for ai, (name, parent, flags, start) in enumerate(anim.joints):
            bp, bq = anim.base[ai]
            tx, ty, tz = bp
            qx, qy, qz = bq
            k = start
            if flags & 1:
                tx = frame[k]; k += 1
            if flags & 2:
                ty = frame[k]; k += 1
            if flags & 4:
                tz = frame[k]; k += 1
            if flags & 8:
                qx = frame[k]; k += 1
            if flags & 16:
                qy = frame[k]; k += 1
            if flags & 32:
                qz = frame[k]; k += 1
            lt = Vector((tx, ty, tz)) * scale
            lq = file_quat(qx, qy, qz)
            if 0 <= parent < ai:
                mt[ai] = mt[parent] + mq[parent] @ lt
                mq[ai] = mq[parent] @ lq
            else:
                mt[ai] = rz @ lt
                mq[ai] = rz @ lq

        world = [None] * n
        for i, j in enumerate(joints):
            ai = src[i]
            if ai is not None:
                world[i] = (mt[ai], mq[ai] @ corr[i])
            elif j.parent >= 0:
                world[i] = xf_mul(world[j.parent], rel[i])
            else:
                world[i] = bone_rest[i]
            if j.parent >= 0:
                basis = xf_mul(rel_inv[i], xf_mul(xf_inv(world[j.parent]), world[i]))
            else:
                basis = xf_mul(rel_inv[i], world[i])
            q = basis[1].normalized()
            if quats[i]:
                q.make_compatible(quats[i][-1])
            locs[i].append(basis[0])
            quats[i].append(q)
    return locs, quats


def new_action(name, arm_obj):
    act = bpy.data.actions.new(name)
    act.use_fake_user = True
    slot = act.slots.new(id_type='OBJECT', name=arm_obj.name)
    layer = act.layers.new("Layer")
    strip = layer.strips.new(type='KEYFRAME')
    bag = strip.channelbag(slot, ensure=True)
    return act, slot, bag


_INTERP_LINEAR = 1  # BEZT_IPO_LIN


def write_bone_curves(bag, bone_name, frames, locs, quats):
    esc = bpy.utils.escape_identifier(bone_name)
    count = len(frames)
    for prop, comps, data in (("location", 3, locs),
                              ("rotation_quaternion", 4, quats)):
        path = 'pose.bones["%s"].%s' % (esc, prop)
        for c in range(comps):
            fc = bag.fcurves.new(path, index=c, group_name=bone_name)
            kp = fc.keyframe_points
            kp.add(count)
            co = [0.0] * (2 * count)
            co[0::2] = frames
            co[1::2] = [d[c] for d in data]
            kp.foreach_set("co", co)
            kp.foreach_set("interpolation", [_INTERP_LINEAR] * count)
            fc.update()


def assign_action(arm_obj, act, slot):
    ad = arm_obj.animation_data or arm_obj.animation_data_create()
    ad.action = act
    try:
        ad.action_slot = slot
    except Exception as e:
        print("MD5 Import: could not bind slot for '%s': %s" % (act.name, e))


# ---------------------------------------------------------------------------
# Folder scan for the animation list
# ---------------------------------------------------------------------------

def _list_anim_files(folder, recursive):
    out = []
    if recursive:
        for root, _dirs, files in os.walk(folder):
            for f in files:
                if f.lower().endswith(".md5anim"):
                    out.append(os.path.join(root, f))
    else:
        try:
            names = os.listdir(folder)
        except OSError:
            return out
        for f in names:
            p = os.path.join(folder, f)
            if f.lower().endswith(".md5anim") and os.path.isfile(p):
                out.append(p)
    return out


def scan_anims(op, mesh_path):
    """Fill the operator's anim list from the .md5mesh's folder, checking each
    anim's joint hierarchy against the mesh."""
    keep = {it.filepath for it in op.md5anims if it.use}
    op.md5anims.clear()
    try:
        _v, mesh_joints, _m = parse_md5mesh(mesh_path, joints_only=True)
    except (OSError, MD5ParseError) as e:
        print("MD5 Import: %s" % e)
        mesh_joints = None
    names = [j.name for j in mesh_joints] if mesh_joints else []
    name_set = set(names)
    parents = [mesh_joints[j.parent].name if j.parent >= 0 else ""
               for j in mesh_joints] if mesh_joints else []

    folder = os.path.dirname(mesh_path)
    files = _list_anim_files(folder, op.include_subfolders)
    files.sort(key=lambda p: os.path.relpath(p, folder).lower())
    for path in files:
        it = op.md5anims.add()
        it.name = os.path.splitext(os.path.relpath(path, folder))[0].replace("\\", "/")
        it.filepath = path
        try:
            a = parse_md5anim(path, header_only=True)
        except (OSError, MD5ParseError) as e:
            it.status = 'ERROR'
            it.info = "unreadable"
            print("MD5 Import: %s" % e)
            continue
        info = "%d fr, %d fps" % (a.num_frames, a.frame_rate)
        a_names = [j[0] for j in a.joints]
        a_parents = [a.joints[j[1]][0] if 0 <= j[1] < len(a.joints) else ""
                     for j in a.joints]
        missing = sum(1 for nm in a_names if nm not in name_set)
        if mesh_joints is None:
            it.status = 'WARN'
        elif a_names == names and a_parents == parents:
            it.status = 'OK'
        elif missing == len(a_names):
            it.status = 'NONE'
            info += ", no joints match"
        elif missing:
            it.status = 'MISSING'
            info += ", %d joints not in mesh" % missing
        else:
            # every joint found, but the engine also wants the same count,
            # order and parents (idMD5Anim::CheckModelHierarchy)
            it.status = 'WARN'
            info += ", hierarchy differs"
        it.info = info
        it.use = path in keep and it.status != 'NONE'


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------

def import_md5(context, op):
    mesh_path = op.filepath
    version, joints, meshes = parse_md5mesh(mesh_path)
    stem = os.path.splitext(os.path.basename(mesh_path))[0]
    scale = op.import_scale
    rz = Quaternion((0.0, 0.0, 1.0), op.rotate_z)

    if context.view_layer.objects.active and \
       context.view_layer.objects.active.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')
    for o in context.selected_objects:
        o.select_set(False)

    # all md5mesh joints are in model space: scale and turn every one
    jt = [rz @ (j.pos * scale) for j in joints]
    jq = [(rz @ j.quat).normalized() for j in joints]

    deform = {ji for mb in meshes for ji, bias, _off in mb.weights if bias > 0.0}
    arm_obj, bone_names, rest, corr = build_armature(
        context, stem, joints, jt, jq, op.fix_bone_orientation, deform)
    arm_obj["md5_import_scale"] = scale
    arm_obj["md5_import_rotate_z"] = op.rotate_z

    objs = []
    for i, mb in enumerate(meshes):
        obj = build_mesh_object(context, mb, i, stem, joints, jt, jq, bone_names,
                                scale, arm_obj, op.merge_vertices,
                                op.import_normals)
        if obj:
            objs.append(obj)

    # --- animations ---
    wanted = [it for it in op.md5anims if it.use and it.status not in ('NONE', 'ERROR')]
    anims = []
    for it in wanted:
        try:
            a = parse_md5anim(it.filepath)
        except (OSError, MD5ParseError) as e:
            op.report({'WARNING'}, "Skipped %s: %s" % (it.name, e))
            continue
        if not a.frames:
            op.report({'WARNING'}, "Skipped %s: no frames" % it.name)
            continue
        if it.status != 'OK':
            op.report({'WARNING'}, "%s: %s" % (it.name, it.info))
        anims.append((os.path.splitext(os.path.basename(it.filepath))[0], a))

    first_action = None
    first_range = None
    start = op.start_frame
    if anims:
        rates = {a.frame_rate for _n, a in anims}
        if len(rates) > 1:
            op.report({'WARNING'}, "Anims use different frame rates %s; "
                      "the scene can only have one" % sorted(rates))
        if op.anim_mode == 'SEPARATE':
            for name, a in anims:
                locs, quats = anim_bone_keys(a, joints, rest, corr, scale, rz)
                act, slot, bag = new_action(name, arm_obj)
                frames = [float(start + f) for f in range(a.num_frames)]
                for i, bname in enumerate(bone_names):
                    write_bone_curves(bag, bname, frames, locs[i], quats[i])
                if first_action is None:
                    first_action = (act, slot)
                    first_range = (start, start + a.num_frames - 1)
        else:
            act, slot, bag = new_action(stem + "_all", arm_obj)
            frames = []
            all_locs = [[] for _ in joints]
            all_quats = [[] for _ in joints]
            f0 = start
            for name, a in anims:
                locs, quats = anim_bone_keys(a, joints, rest, corr, scale, rz)
                act.pose_markers.new(name).frame = f0
                frames.extend(float(f0 + f) for f in range(a.num_frames))
                for i in range(len(joints)):
                    # keep the whole clip on the previous clip's hemisphere
                    if all_quats[i] and quats[i] and \
                       quats[i][0].dot(all_quats[i][-1]) < 0.0:
                        quats[i] = [-q for q in quats[i]]
                    all_locs[i].extend(locs[i])
                    all_quats[i].extend(quats[i])
                f0 += a.num_frames
            # the exporter takes an action's range from its pose markers when
            # it has two or more, so close the last clip with an end marker
            act.pose_markers.new("end").frame = f0 - 1
            for i, bname in enumerate(bone_names):
                write_bone_curves(bag, bname, frames, all_locs[i], all_quats[i])
            first_action = (act, slot)
            first_range = (start, f0 - 1)

        assign_action(arm_obj, *first_action)
        if op.set_scene:
            scn = context.scene
            scn.render.fps = anims[0][1].frame_rate
            scn.render.fps_base = 1.0
            scn.frame_start, scn.frame_end = first_range
            scn.frame_set(first_range[0])

    context.view_layer.objects.active = arm_obj
    arm_obj.select_set(True)
    return "MD5 v%d: %d bones, %d meshes, %d anims" % (
        version, len(joints), len(objs), len(anims))


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

class MD5IMP_AnimItem(bpy.types.PropertyGroup):
    filepath: StringProperty()
    use: BoolProperty(name="", default=False)
    status: StringProperty()
    info: StringProperty()


_STATUS_ICON = {
    'OK': 'CHECKMARK',
    'WARN': 'ERROR',
    'MISSING': 'ERROR',
    'NONE': 'CANCEL',
    'ERROR': 'CANCEL',
}


class MD5IMP_UL_Anims(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon,
                  active_data, active_propname, index):
        row = layout.row(align=True)
        sub = row.row(align=True)
        sub.enabled = item.status not in ('NONE', 'ERROR')
        sub.prop(item, "use", text=item.name)
        info = row.row()
        info.alignment = 'RIGHT'
        info.label(text=item.info, icon=_STATUS_ICON.get(item.status, 'NONE'))


def _browser_operator(context):
    space = context.space_data
    op = getattr(space, "active_operator", None) if space else None
    return op or getattr(context, "active_operator", None)


def _browser_selected_path(context):
    space = context.space_data
    if not space or space.type != 'FILE_BROWSER' or not space.params:
        return None
    d = space.params.directory
    if isinstance(d, bytes):
        d = os.fsdecode(d)
    return os.path.join(d, space.params.filename)


class MD5IMP_OT_SelectAnims(bpy.types.Operator):
    """(De-)Select all animations or invert the selection"""
    bl_idname = "import_scene.idtech4_md5_select_anims"
    bl_label = "Select animations"
    bl_options = {'INTERNAL'}

    action: EnumProperty(
        items=(("SELECT", "Select all", ""),
               ("DESELECT", "Deselect all", ""),
               ("INVERT", "Invert selection", "")),
        default="SELECT")

    def execute(self, context):
        op = _browser_operator(context)
        if op is None or not hasattr(op, "md5anims"):
            return {'CANCELLED'}
        for it in op.md5anims:
            if it.status in ('NONE', 'ERROR'):
                continue
            if self.action == "DESELECT":
                it.use = False
            elif self.action == "INVERT":
                it.use = not it.use
            else:
                it.use = True
        return {'FINISHED'}


class MD5IMP_OT_ScanAnims(bpy.types.Operator):
    """List the .md5anim files next to the selected .md5mesh"""
    bl_idname = "import_scene.idtech4_md5_scan_anims"
    bl_label = "Scan animations"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        op = _browser_operator(context)
        if op is None or not hasattr(op, "md5anims"):
            return {'CANCELLED'}
        path = _browser_selected_path(context) or op.filepath
        if not (path and path.lower().endswith(".md5mesh") and os.path.isfile(path)):
            self.report({'WARNING'}, "Select an .md5mesh file first")
            return {'CANCELLED'}
        scan_anims(op, path)
        op.scanned_key = "%s|%d" % (path, op.include_subfolders)
        return {'FINISHED'}


class MD5IMP_OT_Import(bpy.types.Operator, ImportHelper):
    """Import an idTech 4 MD5 mesh with a chosen set of its animations"""
    bl_idname = "import_scene.idtech4_md5"
    bl_label = "Import MD5"
    bl_options = {'REGISTER', 'UNDO'}
    filename_ext = ".md5mesh"

    filter_glob: StringProperty(default="*.md5mesh", options={'HIDDEN'})

    import_scale: FloatProperty(
        name="Scale", description="Scale the mesh, skeleton and animations",
        default=1.0, min=1e-6, soft_max=100.0, precision=5)

    rotate_z: FloatProperty(
        name="Rotate Z",
        description="Turn the model and its animations about the vertical axis",
        default=0.0, subtype='ANGLE', soft_min=-math.pi, soft_max=math.pi)

    fix_bone_orientation: BoolProperty(
        name="Fix bone orientation",
        description="Point each bone at its child, the way bones built in "
                    "Blender look, instead of along the joint's own Y axis. "
                    "The mesh and animation do not change, but a re-export "
                    "writes the new joint axes - check anything that reads "
                    "joint axes in-game (muzzle/barrel joints, attachments)",
        default=False)

    merge_vertices: BoolProperty(
        name="Weld seams",
        description="Merge verts the MD5 split for UVs/normals when they have "
                    "the same position and weights (UVs and normals are kept "
                    "per corner)",
        default=True)

    import_normals: BoolProperty(
        name="v12 normals",
        description="Use the normals stored in MD5Version 12 files as custom "
                    "normals",
        default=True)

    anim_mode: EnumProperty(
        name="Actions",
        items=(("SEPARATE", "One per file",
                "One Action per animation, named after the file"),
               ("SINGLE", "All in one",
                "All animations back to back in one Action, with a pose "
                "marker at the start of each")),
        default="SEPARATE")

    start_frame: IntProperty(
        name="Start frame", description="Frame the first anim frame is keyed on",
        default=1)

    set_scene: BoolProperty(
        name="Set scene frame rate/range",
        description="Set the scene frame rate from the first animation and "
                    "the frame range to the active Action",
        default=True)

    include_subfolders: BoolProperty(
        name="Subfolders",
        description="Also list .md5anim files in folders below the mesh",
        default=False)

    md5anims: CollectionProperty(type=MD5IMP_AnimItem)
    md5anims_idx: IntProperty()
    scanned_key: StringProperty(options={'HIDDEN', 'SKIP_SAVE'})

    def check(self, context):
        path = self.filepath
        key = "%s|%d" % (path, self.include_subfolders)
        if key == self.scanned_key:
            return False
        if not (path.lower().endswith(".md5mesh") and os.path.isfile(path)):
            return False
        self.scanned_key = key
        scan_anims(self, path)
        return True

    def draw(self, context):
        layout = self.layout

        box = layout.box()
        box.label(text="Mesh and skeleton", icon='ARMATURE_DATA')
        box.prop(self, "import_scale")
        box.prop(self, "rotate_z")
        box.prop(self, "fix_bone_orientation")
        box.prop(self, "merge_vertices")
        box.prop(self, "import_normals")

        box = layout.box()
        box.label(text="Animations", icon='ACTION')
        row = box.row(align=True)
        row.operator(MD5IMP_OT_ScanAnims.bl_idname, text="Scan", icon='FILE_REFRESH')
        row.prop(self, "include_subfolders", toggle=True)
        n = len(self.md5anims)
        if n == 0:
            box.label(text="Select an .md5mesh to list its anims")
        else:
            box.template_list("MD5IMP_UL_Anims", "", self, "md5anims",
                              self, "md5anims_idx", rows=min(max(n, 3), 10))
            row = box.row(align=True)
            row.operator(MD5IMP_OT_SelectAnims.bl_idname, text="All").action = "SELECT"
            row.operator(MD5IMP_OT_SelectAnims.bl_idname, text="None").action = "DESELECT"
            row.operator(MD5IMP_OT_SelectAnims.bl_idname, text="Invert").action = "INVERT"
            box.label(text="%d of %d selected" % (
                sum(1 for it in self.md5anims if it.use), n))
        box.row().prop(self, "anim_mode", expand=True)
        box.prop(self, "start_frame")
        box.prop(self, "set_scene")

    def execute(self, context):
        if not self.filepath.lower().endswith(".md5mesh"):
            self.report({'ERROR'}, "Pick an .md5mesh file")
            return {'CANCELLED'}
        try:
            msg = import_md5(context, self)
        except (OSError, MD5ParseError) as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
        self.report({'INFO'}, msg)
        print("MD5 Import: " + msg)
        return {'FINISHED'}


def menu_func(self, context):
    self.layout.operator(MD5IMP_OT_Import.bl_idname,
                         text="idTech 4 MD5 (.md5mesh/.md5anim)")


classes = (
    MD5IMP_AnimItem,
    MD5IMP_UL_Anims,
    MD5IMP_OT_SelectAnims,
    MD5IMP_OT_ScanAnims,
    MD5IMP_OT_Import,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.TOPBAR_MT_file_import.append(menu_func)


def unregister():
    bpy.types.TOPBAR_MT_file_import.remove(menu_func)
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
