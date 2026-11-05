"""개발자용: AI(TRELLIS 등)로 만든 까치 3D 모델을 앱에 쓰기 좋게 다듬는다.

AI 모델은 표면이 잘게 엉켜 있고(겹친 삼각형), 바늘구멍 같은 틈이 있고, 텍스처가 수백 조각으로
쪼개져 있어서 그대로 쓰면 흰 점이 생긴다. 그래서
  1) 텍스처 색을 꼭짓점 색으로 옮기고
  2) 모델을 작은 상자(복셀)로 채운 뒤 겉면을 새로 떠서(마칭 큐브) 구멍 없이 닫힌 표면을 만들고
     (Poisson 재구성은 이 모델들에서 자꾸 죽어서 쓰지 않는다)
  3) 면 수를 줄이고 잔물결을 편 뒤
  4) 원래 표면에서 가장 가까운 점들의 색을 가져와 칠한다
결과는 텍스처 없이 꼭짓점 색(COLOR_0)만 있는 GLB. 검정/흰색 정리와 털 재질은 화면(three.js)이 맡는다.

    uv run --no-project --python 3.12 --with pymeshlab --with trimesh --with numpy --with pillow --with scipy \\
        --with scikit-image python scripts/clean_model.py 원본.glb 결과.glb [--faces 50000] [--res 220]
"""

import argparse

import numpy as np
import pymeshlab
import trimesh
from scipy import ndimage
from scipy.spatial import cKDTree
from skimage import measure


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("dst")
    ap.add_argument("--faces", type=int, default=50000)
    ap.add_argument("--res", type=int, default=220, help="가장 긴 변을 몇 칸으로 나눌지 (클수록 세밀)")
    args = ap.parse_args()

    mesh = trimesh.load(args.src, force="mesh")
    colors = mesh.visual.to_color().vertex_colors[:, :3].astype(np.float64) / 255.0   # 텍스처 → 꼭짓점 색
    print(f"원본: 꼭짓점 {len(mesh.vertices):,} / 면 {len(mesh.faces):,}", flush=True)

    # 2) 복셀로 채우기 → 겉면 다시 뜨기
    pitch = mesh.extents.max() / args.res
    vox = mesh.voxelized(pitch)
    grid = ndimage.binary_closing(vox.matrix, iterations=2)       # 바늘구멍 메우기
    # 머리 꼭대기(위 15%)는 원본에 움푹한 홈이 있어서 더 세게 메운다
    # (몸 전체를 세게 메우면 펼친 날개와 몸 사이가 붙어버린다)
    top = int(grid.shape[1] * 0.85)
    strong = ndimage.binary_closing(np.pad(vox.matrix, 8), iterations=7)[8:-8, 8:-8, 8:-8]
    grid[:, top:, :] |= strong[:, top:, :]
    grid = ndimage.binary_fill_holes(grid)                         # 속 채우기
    grid = np.pad(grid, 2)
    verts, faces, _, _ = measure.marching_cubes(grid.astype(np.float32), level=0.5)
    verts = trimesh.transform_points(verts - 2, vox.transform)     # 칸 번호 → 원래 좌표
    print(f"닫힌 표면: 면 {len(faces):,}", flush=True)

    # 3) 면 줄이기 + 잔물결 펴기
    ms = pymeshlab.MeshSet()
    ms.add_mesh(pymeshlab.Mesh(vertex_matrix=verts, face_matrix=faces[:, ::-1]))   # 마칭 큐브는 안팎이 반대
    ms.meshing_remove_connected_component_by_diameter(mincomponentdiag=pymeshlab.PercentageValue(10))
    ms.apply_coord_taubin_smoothing(stepsmoothnum=12)
    ms.meshing_decimation_quadric_edge_collapse(targetfacenum=args.faces, preservenormal=True, qualitythr=0.5)
    ms.apply_coord_taubin_smoothing(stepsmoothnum=6)
    m = ms.current_mesh()
    v, f = m.vertex_matrix(), m.face_matrix()
    # 면 줄이기에서 가끔 생기는 아주 긴 실 같은 삼각형 (예: 양쪽 날개 끝을 잇는 것) → 날개를 접으면 막대처럼 보인다
    edge = np.max([np.linalg.norm(v[f[:, i]] - v[f[:, (i + 1) % 3]], axis=1) for i in range(3)], axis=0)
    long_ = edge > 0.08 * np.ptp(v, axis=0).max()
    if long_.any():
        print(f"긴 삼각형 {long_.sum()}개 지움")
        f = f[~long_]

    # 4) 색: 원래 표면의 가까운 점 8개 색을 거리로 섞는다
    tree = cKDTree(mesh.vertices)
    dist, idx = tree.query(v, k=8)
    wgt = 1.0 / (dist + 1e-6)
    col = (colors[idx] * wgt[..., None]).sum(1) / wgt.sum(1, keepdims=True)
    c = np.concatenate([(col * 255).astype(np.uint8), np.full((len(v), 1), 255, np.uint8)], axis=1)
    print(f"결과: 꼭짓점 {len(v):,} / 면 {len(f):,} / 크기 {np.ptp(v, axis=0).round(3)} (원본 {mesh.extents.round(3)})")

    trimesh.Trimesh(vertices=v, faces=f, vertex_colors=c, process=False).export(args.dst)
    print(f"저장: {args.dst}")


if __name__ == "__main__":
    main()
