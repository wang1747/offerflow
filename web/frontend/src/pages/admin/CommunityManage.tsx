import { useCallback, useEffect, useState } from "react";
import { api } from "../../api/client";
import type { CommunityPost, CommunityPostDetail } from "../../types";
import { Badge, Card, EmptyState, Loading, Modal, TableScroll } from "../../components/ui";

const POST_CATEGORIES = ["经验分享", "面经", "offer分享", "资源推荐", "其他"];

const formatDate = (iso?: string) => {
  if (!iso) return "-";
  const d = new Date(iso);
  return `${d.toLocaleDateString()} ${d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`;
};

const btnPrimary =
  "rounded-lg bg-brand-600 px-4 py-2 text-sm font-medium text-white transition hover:bg-brand-700 disabled:opacity-50";
const btnGhost =
  "rounded-lg border border-slate-200 px-4 py-2 text-sm font-medium text-slate-600 transition hover:bg-slate-50";
const btnDanger =
  "rounded-lg bg-rose-50 px-3 py-1.5 text-xs font-medium text-rose-600 transition hover:bg-rose-100";

export default function CommunityManage() {
  const [posts, setPosts] = useState<CommunityPost[]>([]);
  const [categoryFilter, setCategoryFilter] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [detail, setDetail] = useState<CommunityPostDetail | null>(null);

  const load = useCallback(async (category = categoryFilter) => {
    setLoading(true);
    setError("");
    try {
      const res = await api.community.listPosts(category || undefined, 1, 100);
      if (res.success && res.data) setPosts(res.data.items);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [categoryFilter]);

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const openDetail = async (id: string) => {
    try {
      const res = await api.community.postDetail(id);
      if (res.success && res.data) setDetail(res.data);
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const removePost = async (id: string, title: string) => {
    if (!window.confirm(`确定删除帖子「${title}」？该操作不可恢复。`)) return;
    try {
      await api.community.deletePost(id);
      setMessage("帖子已删除");
      await load();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const removeComment = async (id: string) => {
    if (!window.confirm("确定删除这条评论？")) return;
    try {
      await api.community.deleteComment(id);
      setMessage("评论已删除");
      if (detail) await openDetail(detail.id);
    } catch (e) {
      setError((e as Error).message);
    }
  };

  return (
    <div>
      {error && (
        <div className="mb-4 flex items-center justify-between rounded-lg bg-rose-50 px-4 py-2 text-sm text-rose-600">
          <span>{error}</span>
          <button className="text-rose-400 hover:text-rose-600" onClick={() => setError("")}>×</button>
        </div>
      )}
      {message && (
        <div className="mb-4 flex items-center justify-between rounded-lg bg-emerald-50 px-4 py-2 text-sm text-emerald-600">
          <span>{message}</span>
          <button className="text-emerald-400 hover:text-emerald-600" onClick={() => setMessage("")}>×</button>
        </div>
      )}

      <div className="mb-4 flex flex-wrap items-center gap-2">
        <span className="mr-1 text-xs text-slate-400">分类</span>
        <button className={`${categoryFilter === "" ? btnPrimary : btnGhost}`}
          onClick={() => { setCategoryFilter(""); load(""); }}>
          全部
        </button>
        {POST_CATEGORIES.map((c) => (
          <button key={c} className={`${categoryFilter === c ? btnPrimary : btnGhost}`}
            onClick={() => { setCategoryFilter(c); load(c); }}>
            {c}
          </button>
        ))}
      </div>

      <Card className="overflow-hidden p-0">
        {loading ? (
          <Loading text="加载帖子列表..." />
        ) : posts.length === 0 ? (
          <div className="p-6">
            <EmptyState title="暂无帖子" description="社区还没有内容" />
          </div>
        ) : (
          <TableScroll>
            <table className="w-full min-w-[720px] text-left text-sm">
              <thead>
                <tr className="border-b border-slate-100 bg-slate-50/80 text-slate-500">
                  <th className="px-5 py-3 font-medium">帖子</th>
                  <th className="px-5 py-3 font-medium">作者</th>
                  <th className="px-5 py-3 font-medium">互动</th>
                  <th className="px-5 py-3 font-medium">时间</th>
                  <th className="px-5 py-3 text-right font-medium">操作</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-50">
                {posts.map((p) => (
                  <tr key={p.id} className="transition hover:bg-slate-50/50">
                    <td className="px-5 py-3.5">
                      <div className="flex items-center gap-2">
                        <Badge color="indigo">{p.category}</Badge>
                        <span className="font-medium text-slate-900">{p.title}</span>
                      </div>
                    </td>
                    <td className="px-5 py-3.5 text-slate-600">{p.author}</td>
                    <td className="px-5 py-3.5 text-slate-500">♥ {p.like_count} · 💬 {p.comment_count}</td>
                    <td className="px-5 py-3.5 text-slate-500">{formatDate(p.created_at)}</td>
                    <td className="px-5 py-3.5 text-right">
                      <div className="flex items-center justify-end gap-2">
                        <button className="of-btn-ghost !px-3 !py-1.5 text-xs" onClick={() => openDetail(p.id)}>
                          查看
                        </button>
                        <button className={btnDanger} onClick={() => removePost(p.id, p.title)}>
                          删除
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </TableScroll>
        )}
      </Card>

      <Modal
        open={!!detail}
        onClose={() => setDetail(null)}
        title={detail?.title || ""}
        footer={<button className={btnGhost} onClick={() => setDetail(null)}>关闭</button>}
      >
        {detail && (
          <div className="space-y-4">
            <div className="flex flex-wrap items-center gap-2 text-xs text-slate-400">
              <Badge color="indigo">{detail.category}</Badge>
              <span>{detail.author}</span>
              <span>{formatDate(detail.created_at)}</span>
            </div>
            <p className="whitespace-pre-wrap text-sm leading-6 text-slate-700">{detail.content}</p>
            <div className="border-t border-slate-100 pt-4">
              <h4 className="mb-3 text-sm font-semibold text-slate-700">评论（{detail.comments.length}）</h4>
              {detail.comments.length === 0 ? (
                <p className="text-xs text-slate-400">暂无评论</p>
              ) : (
                <div className="space-y-2">
                  {detail.comments.map((c) => (
                    <div key={c.id} className="flex items-start justify-between gap-3 rounded-lg bg-slate-50 px-3 py-2">
                      <div className="min-w-0">
                        <div className="flex items-center gap-2 text-xs text-slate-400">
                          <span className="font-medium text-slate-600">{c.author}</span>
                          <span>{formatDate(c.created_at)}</span>
                        </div>
                        <p className="mt-1 text-sm text-slate-700">{c.content}</p>
                      </div>
                      <button className={`${btnDanger} shrink-0`} onClick={() => removeComment(c.id)}>
                        删除
                      </button>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>
        )}
      </Modal>
    </div>
  );
}
