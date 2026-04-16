"""
检查服务端资源状态：健康检查 / 总览统计 / 资源列表 / 资源详情 / 存储桶 / 语义搜索。

用法：
  python check_server.py                         # 完整检查（健康+统计+资源列表+存储桶）
  python check_server.py --health                 # 仅健康检查
  python check_server.py --stats                  # 总览统计（DB + Milvus + S3 计数）
  python check_server.py --resources              # 资源列表（分页）
  python check_server.py --detail <resource_id>   # 查看某个资源的完整详情
  python check_server.py --storage                # MinIO 存储桶文件列表
  python check_server.py --search "角色模型"      # 语义搜索测试
"""
from __future__ import annotations

import argparse
import json
import sys

import requests


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _hr(title: str):
    print(f"\n{'─' * 20} {title} {'─' * 20}")


def _human_size(size) -> str:
    size = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


G = "\033[92m"
R = "\033[91m"
Y = "\033[93m"
W = "\033[0m"


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

def check_health(server: str) -> bool:
    _hr("服务端健康检查")
    try:
        r = requests.get(f"{server}/health", timeout=10)
        r.raise_for_status()
        h = r.json()
        overall = h.get("status", "unknown")
        color = G if overall == "ok" else R
        print(f"  整体状态: {color}{overall}{W}")
        for comp in ("postgres", "milvus", "s3"):
            c = h.get(comp, {})
            st = c.get("status", "unknown")
            det = c.get("detail", "")
            tag = f"{G}OK{W}" if st == "ok" else f"{R}{st}{W}"
            line = f"  {comp:>10}: {tag}"
            if det:
                line += f"  {det[:80]}"
            print(line)
        return True
    except Exception as e:
        print(f"  {R}无法连接服务端: {e}{W}")
        return False


# ---------------------------------------------------------------------------
# Stats (DB + Milvus + S3 aggregate counts)
# ---------------------------------------------------------------------------

def check_stats(server: str):
    _hr("总览统计")
    try:
        r = requests.get(f"{server}/stats", timeout=15)
        r.raise_for_status()
        d = r.json()

        print(f"  数据库资源总数:  {d.get('db_resource_count', '?')}")
        states = d.get("db_state_counts", {})
        if states:
            parts = [f"{k}={v}" for k, v in states.items()]
            print(f"  按状态分布:      {', '.join(parts)}")

        print(f"  Milvus 集合:     {d.get('milvus_collection', '?')}")
        print(f"  Milvus 向量数:   {d.get('milvus_vector_count', '?')}")

        bucket = d.get("s3_bucket", "")
        if bucket:
            print(f"  S3 桶:           {bucket}  (详情请用 --storage)")
    except Exception as e:
        print(f"  {R}获取统计失败: {e}{W}")


# ---------------------------------------------------------------------------
# Resource list (paginated, from the new GET /resources endpoint)
# ---------------------------------------------------------------------------

def check_resources(server: str, page: int = 1, page_size: int = 50):
    _hr("资源列表")
    try:
        r = requests.get(
            f"{server}/resources",
            params={"page": page, "page_size": page_size},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()

        total = data.get("total", 0)
        resources = data.get("resources", [])
        print(f"  共 {total} 个资源  (第 {page} 页, 每页 {page_size})\n")

        if not resources:
            print("  (暂无资源)")
            return

        hdr = (f"  {'resource_id':<40} {'type':<8} {'state':<14} "
               f"{'files':>5} {'prev':>5} {'desc':>5} {'vec':>5}  {'updated_at'}")
        print(hdr)
        print(f"  {'─'*40} {'─'*8} {'─'*14} {'─'*5} {'─'*5} {'─'*5} {'─'*5}  {'─'*19}")
        for res in resources:
            rid = (res.get("resource_id") or "-")[:38]
            rtype = res.get("resource_type", "")
            state = res.get("process_state", "")
            fc = res.get("file_count", 0)
            pc = res.get("preview_count", 0)
            desc_ok = "Y" if res.get("has_description") else "-"
            embed_ok = "Y" if res.get("has_embedding") else "-"
            updated = res.get("updated_at", "")
            print(f"  {rid:<40} {rtype:<8} {state:<14} {fc:>5} {pc:>5} {desc_ok:>5} {embed_ok:>5}  {updated}")
    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            print(f"  {Y}GET /resources 不存在，请确认服务端已更新{W}")
        else:
            print(f"  {R}查询失败: {e}{W}")
    except Exception as e:
        print(f"  {R}查询失败: {e}{W}")


# ---------------------------------------------------------------------------
# Resource detail
# ---------------------------------------------------------------------------

def check_detail(server: str, resource_id: str):
    _hr(f"资源详情: {resource_id}")
    try:
        r = requests.get(f"{server}/resources/{resource_id}", timeout=15)
        r.raise_for_status()
        d = r.json()

        print(f"  resource_id:   {d.get('resource_id')}")
        print(f"  content_md5:   {d.get('content_md5')}")
        print(f"  resource_type: {d.get('resource_type')}")
        print(f"  process_state: {d.get('process_state')}")
        print(f"  source_dir:    {d.get('source_directory', '')}")
        print(f"  source_res_id: {d.get('source_resource_id', '')}")
        print(f"  title:         {d.get('title', '')}")
        print(f"  source:        {d.get('source', '')}")
        print(f"  pack_name:     {d.get('pack_name', '')}")
        print(f"  resource_path: {d.get('resource_path', '')}")
        print(f"  created_at:    {d.get('created_at')}")
        print(f"  updated_at:    {d.get('updated_at')}")
        print(f"  download_name: {d.get('download_file_name', '')}")
        print(f"  download_type: {d.get('download_content_type', '')}")
        print(f"  download_size: {_human_size(d.get('download_file_size', 0))}")

        parent_resource_id = d.get("parent_resource_id")
        if parent_resource_id:
            print(f"  parent_id:     {parent_resource_id}")
        if d.get("child_resource_count", 0):
            print(f"  child_count:   {d.get('child_resource_count', 0)}")
        if d.get("contains_resource_types"):
            print(f"  child_types:   {', '.join(d.get('contains_resource_types', []))}")

        files = d.get("files", [])
        if files:
            print(f"\n  文件 ({len(files)}):")
            for f in files:
                ks3 = f.get("ks3_key") or "(未上传)"
                print(f"    {f['file_name']:<30} {f['file_format']:<6} {_human_size(f['file_size']):>10}  ks3={ks3}")

        previews = d.get("previews", [])
        if previews:
            print(f"\n  预览 ({len(previews)}):")
            for p in previews:
                dim = f"{p.get('width', '?')}x{p.get('height', '?')}" if p.get("width") else "-"
                print(f"    role={p['role']:<10} strategy={p['strategy']:<8} format={p.get('format') or '-':<6} {dim}")

        desc = d.get("description")
        if desc:
            print(f"\n  描述:")
            print(f"    主体: {desc.get('main_content', '')[:100]}")
            print(f"    细节: {desc.get('detail_content', '')[:100]}")
        else:
            print(f"\n  描述: {R}无{W}")

        embed = d.get("embedding")
        if embed:
            print(f"\n  向量:")
            print(f"    dimension={embed.get('dimension')}  checksum={embed.get('checksum')}  model={embed.get('model_version')}")
        else:
            print(f"\n  向量: {R}无{W}")

        err = d.get("last_error", "")
        if err:
            print(f"\n  最后错误: {R}{err}{W}")

    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            print(f"  {R}资源不存在{W}")
        else:
            print(f"  {R}查询失败: {e}{W}")
    except Exception as e:
        print(f"  {R}查询失败: {e}{W}")


# ---------------------------------------------------------------------------
# MinIO storage
# ---------------------------------------------------------------------------

def check_storage(endpoint: str = "http://localhost:9000", access_key: str = "minioadmin",
                   secret_key: str = "minioadmin", bucket: str = "resources"):
    _hr("MinIO 存储桶文件")
    try:
        import boto3
        from botocore.config import Config as BotoConfig

        s3 = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            config=BotoConfig(signature_version="s3v4"),
            region_name="us-east-1",
        )
        resp = s3.list_objects_v2(Bucket=bucket, MaxKeys=200)
        contents = resp.get("Contents", [])
        total_size = sum(o.get("Size", 0) for o in contents)
        print(f"  桶名: {bucket}")
        print(f"  文件数: {len(contents)}")
        print(f"  总大小: {_human_size(total_size)}")
        if contents:
            print(f"\n  {'Key':<60} {'大小':>10} {'修改时间'}")
            print(f"  {'─'*60} {'─'*10} {'─'*20}")
            for obj in contents[:50]:
                key = obj["Key"]
                if len(key) > 58:
                    key = "…" + key[-57:]
                print(f"  {key:<60} {_human_size(obj['Size']):>10} {obj['LastModified'].strftime('%Y-%m-%d %H:%M')}")
            if len(contents) > 50:
                print(f"  … 还有 {len(contents) - 50} 个文件")
        else:
            print("  (桶为空)")
    except ImportError:
        print(f"  {Y}需要安装 boto3: pip install boto3{W}")
    except Exception as e:
        print(f"  {R}无法连接 MinIO: {e}{W}")


# ---------------------------------------------------------------------------
# Semantic search
# ---------------------------------------------------------------------------

def check_search(server: str, query: str, threshold: float = 0.5, top_k: int = 5):
    _hr(f"语义搜索测试: '{query}'")
    try:
        body = {
            "query_text": query,
            "top_k": top_k,
            "similarity_threshold": threshold,
        }
        r = requests.post(f"{server}/search", json=body, timeout=15)
        r.raise_for_status()
        data = r.json()
        results = data.get("results", [])
        print(f"  匹配结果: {len(results)} 条 (threshold={threshold}, top_k={top_k})")
        for i, res in enumerate(results, 1):
            print(f"  [{i}] score={res.get('score', 0):.4f}  type={res.get('resource_type', '')}  "
                  f"id={res.get('resource_id', '')[:20]}  {res.get('description_summary', '')[:40]}")
            preview_url = res.get("primary_preview_url", "")
            download_url = res.get("file_download_url", "")
            parent_download_url = res.get("parent_download_url", "")
            if preview_url:
                print(f"      preview_url: {preview_url}")
            if download_url:
                print(f"      download_url: {download_url}")
            if res.get("parent_resource_id"):
                print(f"      parent_id: {res.get('parent_resource_id')}  parent_title: {res.get('parent_title', '')}")
            if parent_download_url:
                print(f"      parent_download_url: {parent_download_url}")
        if not results:
            sug = data.get("suggestion")
            if sug:
                print(f"  建议: {json.dumps(sug, ensure_ascii=False)}")
    except Exception as e:
        print(f"  搜索失败: {e}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="检查服务端资源状态",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--server", default="http://localhost:8000")
    parser.add_argument("--health", action="store_true", help="仅健康检查")
    parser.add_argument("--stats", action="store_true", help="总览统计")
    parser.add_argument("--resources", action="store_true", help="资源列表")
    parser.add_argument("--detail", type=str, default=None, metavar="RESOURCE_ID",
                        help="查看某个资源的完整详情")
    parser.add_argument("--storage", action="store_true", help="MinIO 存储桶")
    parser.add_argument("--search", type=str, default=None, help="语义搜索测试")
    parser.add_argument("--search-threshold", type=float, default=0.5, help="语义搜索最低分阈值")
    parser.add_argument("--search-top-k", type=int, default=5, help="语义搜索返回条数")
    parser.add_argument("--page", type=int, default=1)
    parser.add_argument("--page-size", type=int, default=50)
    parser.add_argument("--s3-endpoint", default="http://localhost:9000")
    parser.add_argument("--s3-access-key", default="minioadmin")
    parser.add_argument("--s3-secret-key", default="minioadmin")
    parser.add_argument("--s3-bucket", default="resources")
    args = parser.parse_args()

    any_flag = args.health or args.stats or args.resources or args.storage or args.search or args.detail
    show_all = not any_flag

    print("=" * 60)
    print("  ResourceUpload 服务端状态检查")
    print(f"  服务端: {args.server}")
    print("=" * 60)

    if show_all or args.health:
        check_health(args.server)

    if show_all or args.stats:
        check_stats(args.server)

    if show_all or args.resources:
        check_resources(args.server, args.page, args.page_size)

    if args.detail:
        check_detail(args.server, args.detail)

    if show_all or args.storage:
        check_storage(args.s3_endpoint, args.s3_access_key, args.s3_secret_key, args.s3_bucket)

    if args.search:
        check_search(args.server, args.search, args.search_threshold, args.search_top_k)

    print()


if __name__ == "__main__":
    main()
