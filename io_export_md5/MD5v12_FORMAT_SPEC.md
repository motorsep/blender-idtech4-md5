# MD5 Version 12 Format Specification

**Version:** 1.0
**Date:** March 2026
**Status:** Implementation draft — exporter complete, engine support pending

---

## Overview

MD5 Version 12 is a backward-compatible extension of the idTech 4 MD5 format (version 10) that adds per-vertex normals, MikkTSpace tangent frames, and vertex colors to `md5mesh` files. The `md5anim` format is structurally identical between v10 and v12 — only the version number in the header changes.

The goal is to eliminate visual artifacts at UV seams (caused by the engine deriving normals/tangents from geometry) and to enable correct normal map rendering when assets are baked in modern DCC tools (Blender, Substance Painter, Marmoset, xNormal) that use MikkTSpace as the standard tangent basis.

---

## What Changed from Version 10

| Feature | v10 | v12 |
|---------|-----|-----|
| `MD5Version` header | `10` | `12` |
| Per-vertex normal | Not stored; derived from geometry | Stored in bone-local space |
| Per-vertex tangent | Not stored; derived from geometry | MikkTSpace tangent in bone-local space |
| Bitangent sign | Not stored | Stored as 4th tangent component (±1) |
| Vertex colors | Not possible (color/color2 used for skinning) | Optional `vertexcolor` block per mesh |
| `vert` line format | `( u v ) firstWeight weightCount` | `( u v ) firstWeight weightCount ( nx ny nz ) ( tx ty tz tw )` |
| `md5anim` format | unchanged | unchanged (only version number differs) |
| joints block | unchanged | unchanged |
| tris block | unchanged | unchanged |
| weights block | unchanged | unchanged |

---

## File Format: md5mesh

### Header

```
MD5Version 12
commandline "Exported from Blender by io_export_md5.py"

numJoints 71
numMeshes 1
```

The only difference from v10 is the version number. The parser should branch on this value.

### Joints Block

Identical to v10. No changes.

```
joints {
    "origin"  -1 ( 0.000000 0.000000 0.000000 ) ( -0.500000 -0.500000 -0.500000 )    //
    "Body"     0 ( 0.000000 0.000000 36.882813 ) ( -0.500000 -0.500000 -0.500000 )     // origin
    ...
}
```

### Mesh Block

```
mesh {
    shader "models/monsters/imp/skin"

    numverts 891
    vert 0 ( 0.394531 0.248016 ) 0 2 ( 0.000000 0.707107 0.707107 ) ( 0.707107 0.000000 0.000000 1.000000 )
    vert 1 ( 0.500000 0.300000 ) 2 1 ( 0.000000 1.000000 0.000000 ) ( 1.000000 0.000000 0.000000 -1.000000 )
    ...

    numtris 1346
    tri 0 0 2 1
    ...

    numweights 1401
    weight 0 16 0.750000 ( -1.234567 2.345678 3.456789 )
    ...

    numvertexcolors 891
    vertexcolor 0 ( 1.000000 1.000000 1.000000 1.000000 )
    vertexcolor 1 ( 0.800000 0.200000 0.100000 1.000000 )
    ...
}
```

### Vertex Line Format (v12)

```
vert INDEX ( u v ) FIRST_WEIGHT WEIGHT_COUNT ( nx ny nz ) ( tx ty tz tw )
```

| Field | Type | Description |
|-------|------|-------------|
| `INDEX` | int | Vertex index (0-based) |
| `u v` | float | Texture coordinates (v is flipped: `1.0 - blender_v`) |
| `FIRST_WEIGHT` | int | Index of first weight in weights block |
| `WEIGHT_COUNT` | int | Number of weights for this vertex (max 4) |
| `nx ny nz` | float | Vertex normal in **bone-local space** (unit length) |
| `tx ty tz` | float | MikkTSpace tangent in **bone-local space** (unit length) |
| `tw` | float | Bitangent sign: `+1.0` or `-1.0` |

The normal and tangent are stored relative to the **dominant bone** (the bone with the highest weight influence on this vertex). This matches how weights store position offsets — the engine transforms them back to world space using the same joint matrix.

### Vertex Colors Block (Optional)

The `numvertexcolors` / `vertexcolor` block appears after `numweights` inside the mesh block. It is **optional** — if the source mesh has no vertex color attributes, this block is omitted entirely.

```
vertexcolor INDEX ( r g b a )
```

| Field | Type | Range | Description |
|-------|------|-------|-------------|
| `INDEX` | int | 0..numverts-1 | Must match vertex index |
| `r g b a` | float | 0.0 – 1.0 | Linear color, not sRGB |

If the block is present, it must have exactly `numverts` entries (one per vertex). If a vertex has no color data in the source, it defaults to `( 1.0 1.0 1.0 1.0 )`.

### Triangles Block

Identical to v10. No changes.

### Weights Block

Identical to v10. No changes.

---

## File Format: md5anim

The md5anim format is **structurally identical** between v10 and v12. The only change is the `MD5Version` line reads `12` instead of `10`. This allows the engine to associate the correct mesh version when loading animation data.

All hierarchy, bounds, baseframe, and frame data blocks are unchanged.

---

## Engine Implementation Guide

### Parser Changes (Model_md5.cpp)

The parser needs minimal changes. Here is pseudocode for the key modifications:

#### 1. Version Check

```cpp
// In idRenderModelMD5::LoadModel()
parser.ExpectTokenString( MD5_VERSION_STRING );
version = parser.ParseInt();

if ( version != 10 && version != 12 ) {
    parser.Error( "Invalid version %d. Should be 10 or 12\n", version );
}
bool isV12 = ( version == 12 );
```

#### 2. Extended Vertex Parsing

```cpp
// In idMD5Mesh::ParseMesh(), after parsing UV and weight indices:
idVec3 vertNormal( 0, 0, 1 );
idVec4 vertTangent( 1, 0, 0, 1 );  // xyz = tangent, w = bitangent sign

if ( isV12 ) {
    parser.Parse1DMatrix( 3, vertNormal.ToFloatPtr() );
    parser.Parse1DMatrix( 4, vertTangent.ToFloatPtr() );
}
```

Store these per-vertex so they can be used during `R_BuildDeformInfo` instead of deriving from geometry.

#### 3. Using Stored Normals and Tangents

In v10, the engine calls `R_DeriveTangents` / `R_DeriveUnsmoothedTangents` to compute the TBN frame from triangle geometry. In v12, these are already provided per-vertex:

```cpp
// During base pose construction:
if ( isV12 ) {
    // Transform normal from bone-local to model space using dominant joint
    idJointMat& joint = joints[ dominantJointIndex ];
    idVec3 modelNormal = joint.ToMat3() * storedNormal;
    idVec3 modelTangent = joint.ToMat3() * storedTangent.ToVec3();
    float biTangentSign = storedTangent.w;

    basePose[i].SetNormal( modelNormal );
    basePose[i].SetTangent( modelTangent );
    basePose[i].SetBiTangentSign( biTangentSign );
    // Bitangent = cross( normal, tangent ) * biTangentSign
    // (computed in shader, not stored)
} else {
    // v10 path: derive from geometry as before
}
```

#### 4. Vertex Colors

In v10, `idDrawVert.color` (COLOR0) stores 4 joint indices and `color2` (COLOR1) stores 4 joint weights for GPU skinning. This means **vertex colors cannot coexist with GPU skinning data in the same vertex attributes**.

Options for v12 vertex color support:

**Option A — Extra vertex attribute:**
Add a third color attribute (`COLOR2` or a custom attribute) to the vertex format for actual RGBA vertex color. This requires shader and vertex format changes but is the cleanest solution.

**Option B — Uniform/texture lookup:**
Pack vertex colors into a texture or SSBO that the shader indexes by vertex ID. No vertex format changes needed but requires shader modifications.

**Option C — Bake into diffuse:**
If vertex colors are only used for static tinting/AO, bake them into the diffuse texture at export time. No engine changes needed but loses runtime flexibility.

The exporter stores vertex colors in the file regardless — the engine can choose how to use them.

#### 5. Optional Block Parsing

The `numvertexcolors` block is optional. After parsing weights, check if the next token is `numvertexcolors`:

```cpp
// After parsing weights, still inside mesh { } block:
idToken nextToken;
if ( parser.CheckTokenString( "numvertexcolors" ) ) {
    int numColors = parser.ParseInt();
    for ( int i = 0; i < numColors; i++ ) {
        parser.ExpectTokenString( "vertexcolor" );
        parser.ParseInt();  // index
        idVec4 color;
        parser.Parse1DMatrix( 4, color.ToFloatPtr() );
        // Store color[i] = color
    }
}
```

### Shader Changes

The current shaders already support the TBN frame correctly for MikkTSpace:

```hlsl
// From existing shaders (e.g., interaction_uber.vertex):
float4 normal = vertex.normal * 2.0 - 1.0;
float4 tangent = vertex.tangent * 2.0 - 1.0;
float3 binormal = cross( normal.xyz, tangent.xyz ) * tangent.w;
```

This is exactly the MikkTSpace reconstruction formula: `B = cross(N, T) * sign`. As long as the engine packs the v12 normals/tangents into the same byte-encoded format that the shaders expect (`[0,255]` mapped to `[-1,1]`), the existing shaders will produce correct results with MikkTSpace-baked normal maps **without any shader modifications**.

### GPU Skinning Compatibility

The existing GPU skinning path (`skinning.inc`) transforms position, normal, and tangent by the weighted joint matrices:

```hlsl
// skinning.inc transforms these per-vertex:
modelPosition = weighted_joint_transform * vertex.position;
skinnedNormal = weighted_joint_transform * vertex.normal;
skinnedTangent = weighted_joint_transform * vertex.tangent;
```

Since v12 normals and tangents are stored in bone-local space (relative to the dominant bone), the engine must transform them to model space during base pose setup — the same transform already applied to positions. After that, the existing GPU skinning path handles animation correctly with no changes.

---

## Tangent Space Details

### Why MikkTSpace

MikkTSpace (Morten Mikkelsen's tangent space) is the industry standard for tangent basis computation. When both the baker (Blender, Substance, xNormal) and the runtime engine use MikkTSpace, normal maps render identically to the bake preview. Without matching tangent spaces, normal maps show subtle but visible shading errors, especially at UV seam boundaries.

### What the Exporter Computes

The Blender exporter calls `mesh.calc_tangents(uvmap=uv_layer.name)` which computes MikkTSpace tangents per-loop. For each vertex, the exporter:

1. Gets the MikkTSpace tangent vector (`loop.tangent`) and bitangent sign (`loop.bitangent_sign`) from Blender
2. Transforms the tangent from object space to world space: `T_world = world_matrix_3x3 @ T_object`
3. Transforms from world space to bone-local space: `T_local = inv_bone_matrix_3x3 @ T_world`
4. Stores `T_local` as `( tx ty tz )` and the sign as `tw`

The same transform is applied to the vertex normal.

### Dominant Bone

Normals and tangents are stored relative to the **dominant bone** — the bone with the highest skinning weight for that vertex. This is a simplification (the position uses all weighted bones), but it works well in practice because:

- The dominant bone typically has 60-100% of the weight
- Normal/tangent transforms are rotational only (no translation), so small weight differences produce minimal visual error
- This matches common practice in other engines (UE, Unity)

### Reconstruction in Engine

At load time or per-frame for animated models:

```
N_model = joint_rotation * N_stored
T_model = joint_rotation * T_stored
B_model = cross(N_model, T_model) * bitangent_sign
```

The `[N_model, T_model, B_model]` matrix is the tangent-to-model-space transform used by the normal map shader.

---

## Backward Compatibility

- **v10 meshes load unchanged.** The parser checks `MD5Version` and skips extended vertex parsing for version 10.
- **v12 meshes degrade gracefully.** If the engine doesn't support v12, it will error on the version check. A simple version gate is all that's needed.
- **Animations are interchangeable.** A v12 md5anim can be used with a v10 mesh and vice versa — the animation data is structurally identical. Only the version number in the header differs.
- **The Blender exporter supports both.** The "MD5 Version 12" checkbox defaults to OFF, producing standard v10 files. When checked, it produces v12 with all extended data.

---

## Test Matrix

| Test Case | Expected Result |
|-----------|----------------|
| v10 export (checkbox OFF) | Identical to previous exporter output |
| v12 export, mesh with UVs | Normals + tangents in every vert line |
| v12 export, mesh without UVs | Normals present, tangents default to (1,0,0,1) |
| v12 export, mesh with vertex colors | `numvertexcolors` block present |
| v12 export, mesh without vertex colors | No `numvertexcolors` block |
| v12 md5anim | Structurally identical to v10, version=12 |
| Engine loads v10 | Existing path, no changes needed |
| Engine loads v12 | Extended parser reads normals/tangents/colors |
| Normal map baked in Blender | Renders correctly with MikkTSpace tangents |

---

## File Size Impact

For a typical character mesh (891 verts, 71 joints):

| Component | v10 | v12 | Delta |
|-----------|-----|-----|-------|
| vert line | ~40 chars | ~100 chars | +60 chars/vert |
| vertexcolor line | 0 | ~50 chars | +50 chars/vert |
| Total mesh file | ~120 KB | ~220 KB | ~+80% |
| md5anim file | unchanged | unchanged | 0% |

The size increase is text-only and only affects the human-readable `.md5mesh` file. The engine's binary cache (`md5b`) will pack these efficiently.
