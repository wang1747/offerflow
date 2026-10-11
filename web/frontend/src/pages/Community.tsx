import { type FC, useCallback, useEffect, useState } from "react";
import { api } from "../api/client";
import type { CommunityPost, CommunityPostDetail, CommunityAttachment, Feedback } from "../types";
import { Badge, Card, EmptyState, Modal, PageHeader, Tabs, Icons } from "../components/ui";
import Avatar from "../components/Avatar";
import { useAuthStore } from "../store/authStore";

const POST_CATEGORIES = ["经验分享", "面经", "offer分享", "资源推荐", "其他"];
const FEEDBACK_CATEGORIES = ["功能建议", "问题反馈", "体验优化", "其他"];

const FEEDBACK_STATUS: Record<string, { label: string; color: "gray" | "blue" | "green" | "amber" | "red" }> = {
  pending: { label: "待处理", color: "amber" },
  accepted: { label: "已采纳", color: "green" },
  declined: { label: "已婉拒", color: "gray" },
  replied: { label: "已回复", color: "blue" },
};

const formatDate = (iso?: string) => {
  if (!iso) return "-";
  const d = new Date(iso);
  return `${d.toLocaleDateString()} ${d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`;
};

const btnPrimary =
  "rounded-lg bg-brand-600 px-4 py-2 text-sm font-medium text-white transition hover:bg-brand-700 disabled:opacity-50";
const btnGhost =
  "rounded-lg border border-slate-200 px-4 py-2 text-sm font-medium text-slate-600 transition hover:bg-slate-50";
const inputCls =
  "w-full rounded-lg border border-slate-200 px-3 py-2 text-sm focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500";

const Community: FC = () => {
  const [tab, setTab] = useState<"posts" | "feedback">("posts");

  // 分享墙
  const [posts, setPosts] = useState<CommunityPost[]>([]);
  const [postCategory, setPostCategory] = useState("");
  const [showCreate, setShowCreate] = useState(false);
  const [postForm, setPostForm] = useState({ title: "", content: "", category: "经验分享", tags: "" });
  const [postFiles, setPostFiles] = useState<CommunityAttachment[]>([]);
  const [uploading, setUploading] = useState(false);
  const [detail, setDetail] = useState<CommunityPostDetail | null>(null);
  const [commentText, setCommentText] = useState("");

  // 建议
  const [feedbacks, setFeedbacks] = useState<Feedback[]>([]);
  const [fbCategory, setFbCategory] = useState("");
  const [showFb, setShowFb] = useState(false);
  const [fbForm, setFbForm] = useState({ category: "功能建议", title: "", content: "" });

  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  // 管理员身份：管理员在交流中心内可直接治理（删帖 / 删评论 / 处理反馈）
  const { user } = useAuthStore();
  const isAdmin = user?.role === "admin";

  // 反馈处理弹窗（仅管理员）
  const [fbEditing, setFbEditing] = useState<Feedback | null>(null);
  const [fbEditStatus, setFbEditStatus] = useState("replied");
  const [fbReplyText, setFbReplyText] = useState("");
  const [fbSaving, setFbSaving] = useState(false);

  const loadPosts = useCallback(async (category = postCategory) => {
    try {
      const res = await api.community.listPosts(category || undefined, 1, 50);
      if (res.success && res.data) {
        setPosts(res.data.items);
      }
    } catch (e) {
      setError((e as Error).message);
    }
  }, [postCategory]);

  const loadFeedbacks = useCallback(async (category = fbCategory) => {
    try {
      const res = await api.feedback.list(category || undefined, 1, 50);
      if (res.success && res.data) {
        setFeedbacks(res.data.items);
      }
    } catch (e) {
      setError((e as Error).message);
    }
  }, [fbCategory]);

  useEffect(() => {
    loadPosts();
    loadFeedbacks();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const submitPost = async () => {
    if (!postForm.title.trim() || !postForm.content.trim()) return;
    setLoading(true);
    try {
      const tags = postForm.tags.split(/[,，\s]+/).filter(Boolean);
      await api.community.createPost({
        title: postForm.title, content: postForm.content,
        category: postForm.category, tags,
        attachments: postFiles,
      });
      setShowCreate(false);
      setPostForm({ title: "", content: "", category: "经验分享", tags: "" });
      setPostFiles([]);
      await loadPosts();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  };

  const handleFileSelect = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = e.target.files;
    if (!files || files.length === 0) return;
    setUploading(true);
    setError("");
    try {
      for (const file of Array.from(files)) {
        const res = await api.community.uploadAttachment(file);
        if (res.success && res.data) {
          setPostFiles((prev) => [...prev, res.data as CommunityAttachment]);
        } else {
          setError(res.error || "上传失败");
          break;
        }
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "上传失败");
    } finally {
      setUploading(false);
      e.target.value = "";
    }
  };

  const removeFile = (name: string) => {
    setPostFiles((prev) => prev.filter((f) => f.name !== name));
  };

  const openDetail = async (id: string) => {
    try {
      const res = await api.community.postDetail(id);
      if (res.success && res.data) setDetail(res.data);
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const submitComment = async () => {
    if (!detail || !commentText.trim()) return;
    try {
      await api.community.addComment(detail.id, commentText);
      setCommentText("");
      await openDetail(detail.id);
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const likePost = async (id: string) => {
    try {
      await api.community.toggleLike(id);
      await loadPosts();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const submitFeedback = async () => {
    if (!fbForm.title.trim() || !fbForm.content.trim()) return;
    setLoading(true);
    try {
      await api.feedback.create(fbForm);
      setShowFb(false);
      setFbForm({ category: "功能建议", title: "", content: "" });
      await loadFeedbacks();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  };

  const likeFeedback = async (id: string) => {
    try {
      await api.feedback.toggleLike(id);
      await loadFeedbacks();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  // —— 治理操作（仅管理员）——
  const removePost = async (id: string, title: string) => {
    if (!window.confirm(`确定删除帖子「${title}」？该操作不可恢复。`)) return;
    try {
      await api.community.deletePost(id);
      setError("");
      if (detail && detail.id === id) setDetail(null);
      await loadPosts();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const removeComment = async (id: string) => {
    if (!window.confirm("确定删除这条评论？")) return;
    try {
      await api.community.deleteComment(id);
      setError("");
      if (detail) await openDetail(detail.id);
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const openFeedbackEdit = (f: Feedback) => {
    setFbEditing(f);
    setFbEditStatus(f.status === "pending" ? "replied" : f.status);
    setFbReplyText(f.admin_reply || "");
  };

  const saveFeedback = async () => {
    if (!fbEditing) return;
    setFbSaving(true);
    setError("");
    try {
      await api.feedback.adminUpdate(fbEditing.id, {
        status: fbEditStatus || undefined,
        admin_reply: fbReplyText.trim() || undefined,
      });
      setFbEditing(null);
      await loadFeedbacks();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setFbSaving(false);
    }
  };

  return (
    <div>
      <PageHeader
        title="交流中心"
        subtitle="和同行分享求职经验，给产品提建议"
        icon={Icons.interview}
      />

      <Tabs
        tabs={[
          { key: "posts", label: "分享墙", icon: Icons.interview },
          { key: "feedback", label: "建议反馈", icon: Icons.alert },
        ]}
        active={tab}
        onChange={(k) => setTab(k as "posts" | "feedback")}
      />

      {error && (
        <div className="mb-4 flex items-center justify-between rounded-lg bg-rose-50 px-4 py-2 text-sm text-rose-600">
          <span>{error}</span>
          <button className="text-rose-400 hover:text-rose-600" onClick={() => setError("")}>×</button>
        </div>
      )}

      {tab === "posts" ? (
        <div>
          <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
            <div className="flex flex-wrap gap-2">
              <button className={`${postCategory === "" ? btnPrimary : btnGhost}`} onClick={() => { setPostCategory(""); loadPosts(""); }}>
                全部
              </button>
              {POST_CATEGORIES.map((c) => (
                <button key={c} className={`${postCategory === c ? btnPrimary : btnGhost}`}
                  onClick={() => { setPostCategory(c); loadPosts(c); }}>
                  {c}
                </button>
              ))}
            </div>
            <button className={btnPrimary} onClick={() => setShowCreate(true)}>发布分享</button>
          </div>

          {posts.length === 0 ? (
            <EmptyState icon={Icons.interview} title="还没有分享" description="来发布第一条求职经验或面经吧" />
          ) : (
            <div className="space-y-3">
              {posts.map((p) => (
                <Card key={p.id} hover className="cursor-pointer p-5">
                  <div onClick={() => openDetail(p.id)}>
                    <div className="flex items-start justify-between gap-4">
                      <h3 className="text-base font-semibold leading-6 text-slate-900">{p.title}</h3>
                      <span className="shrink-0">
                        <Badge color="indigo">{p.category}</Badge>
                      </span>
                    </div>
                    <p className="mt-2 line-clamp-2 text-sm leading-6 text-slate-500">{p.content}</p>
                    {p.tags.length > 0 && (
                      <div className="mt-2 flex flex-wrap gap-1.5">
                        {p.tags.map((t) => (
                          <span key={t} className="rounded-full bg-slate-100 px-2 py-0.5 text-xs text-slate-500">#{t}</span>
                        ))}
                      </div>
                    )}
                    {p.attachments.length > 0 && (
                      <div className="mt-2 flex flex-wrap items-center gap-2">
                        {p.attachments.filter((a) => a.kind === "image").slice(0, 3).map((a) => (
                          <img key={a.name} src={a.url} alt={a.original_name}
                            className="h-16 w-16 rounded-lg border border-slate-200 object-cover" />
                        ))}
                        {p.attachments.filter((a) => a.kind === "doc").map((a) => (
                          <span key={a.name} className="flex max-w-xs items-center gap-1 truncate rounded-lg bg-slate-100 px-2 py-1 text-xs text-slate-500">
                            📄 {a.original_name}
                          </span>
                        ))}
                      </div>
                    )}
                    <div className="mt-4 flex items-center gap-5 border-t border-slate-100 pt-3 text-xs text-slate-400">
                      <span className="flex items-center gap-1.5">
                        <Avatar name={p.author} src={p.author_avatar} size={20} />
                        <span className="text-slate-500">{p.author}</span>
                      </span>
                      <span>{formatDate(p.created_at)}</span>
                      <button
                        className="flex items-center gap-1 rounded-md px-1.5 py-0.5 transition hover:bg-rose-50 hover:text-rose-500"
                        onClick={(e) => { e.stopPropagation(); likePost(p.id); }}
                      >
                        ♥ {p.like_count}
                      </button>
                      <span className="flex items-center gap-1">💬 {p.comment_count}</span>
                      {(isAdmin || p.user_id === user?.id) && (
                        <button
                          className="ml-auto flex items-center gap-1 rounded-md px-1.5 py-0.5 text-rose-400 transition hover:bg-rose-50 hover:text-rose-600"
                          onClick={(e) => { e.stopPropagation(); removePost(p.id, p.title); }}
                        >
                          删除
                        </button>
                      )}
                    </div>
                  </div>
                </Card>
              ))}
            </div>
          )}
        </div>
      ) : (
        <div>
          <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
            <div className="flex flex-wrap gap-2">
              <button className={`${fbCategory === "" ? btnPrimary : btnGhost}`} onClick={() => { setFbCategory(""); loadFeedbacks(""); }}>
                全部
              </button>
              {FEEDBACK_CATEGORIES.map((c) => (
                <button key={c} className={`${fbCategory === c ? btnPrimary : btnGhost}`}
                  onClick={() => { setFbCategory(c); loadFeedbacks(c); }}>
                  {c}
                </button>
              ))}
            </div>
            <button className={btnPrimary} onClick={() => setShowFb(true)}>提建议</button>
          </div>

          {feedbacks.length === 0 ? (
            <EmptyState icon={Icons.alert} title="还没有建议" description="你的建议能帮助产品变得更好" />
          ) : (
            <div className="space-y-3">
              {feedbacks.map((f) => {
                const st = FEEDBACK_STATUS[f.status] || FEEDBACK_STATUS.pending;
                return (
                  <Card key={f.id} hover className="p-5">
                    <div className="flex items-start justify-between gap-4">
                      <div className="min-w-0">
                        <div className="flex flex-wrap items-center gap-2">
                          <Badge color="indigo">{f.category}</Badge>
                          <Badge color={st.color}>{st.label}</Badge>
                        </div>
                        <h3 className="mt-2 text-base font-semibold leading-6 text-slate-900">{f.title}</h3>
                      </div>
                      <button className="flex shrink-0 items-center gap-1 rounded-md px-1.5 py-0.5 text-sm text-slate-400 transition hover:bg-rose-50 hover:text-rose-500"
                        onClick={() => likeFeedback(f.id)}>
                        ♥ {f.like_count}
                      </button>
                    </div>
                    <p className="mt-2 whitespace-pre-wrap text-sm leading-6 text-slate-500">{f.content}</p>
                    {f.admin_reply && (
                      <div className="mt-3 rounded-lg bg-brand-50 px-3 py-2 text-sm leading-6 text-slate-700">
                        <span className="font-medium text-brand-600">官方回复：</span>{f.admin_reply}
                      </div>
                    )}
                    <div className="mt-4 flex items-center gap-3 border-t border-slate-100 pt-3 text-xs text-slate-400">
                      <span>{f.author}</span>
                      <span>{formatDate(f.created_at)}</span>
                      {isAdmin && (
                        <button
                          className="ml-auto font-medium text-brand-600 transition hover:text-brand-700"
                          onClick={() => openFeedbackEdit(f)}
                        >
                          处理
                        </button>
                      )}
                    </div>
                  </Card>
                );
              })}
            </div>
          )}
        </div>
      )}

      {/* 发布分享 Modal */}
      <Modal
        open={showCreate}
        onClose={() => setShowCreate(false)}
        title="发布分享"
        footer={
          <>
            <button className={btnGhost} onClick={() => setShowCreate(false)}>取消</button>
            <button className={btnPrimary} onClick={submitPost} disabled={loading}>发布</button>
          </>
        }
      >
        <div className="space-y-3">
          <div>
            <label className="mb-1 block text-sm text-slate-600">分类</label>
            <select className={inputCls} value={postForm.category}
              onChange={(e) => setPostForm({ ...postForm, category: e.target.value })}>
              {POST_CATEGORIES.map((c) => <option key={c} value={c}>{c}</option>)}
            </select>
          </div>
          <div>
            <label className="mb-1 block text-sm text-slate-600">标题</label>
            <input className={inputCls} placeholder="一句话概括你的分享" value={postForm.title}
              onChange={(e) => setPostForm({ ...postForm, title: e.target.value })} />
          </div>
          <div>
            <label className="mb-1 block text-sm text-slate-600">内容</label>
            <textarea className={`${inputCls} min-h-32`} placeholder="详细说说你的经验或经历…" value={postForm.content}
              onChange={(e) => setPostForm({ ...postForm, content: e.target.value })} />
          </div>
          <div>
            <label className="mb-1 block text-sm text-slate-600">标签（可选，逗号分隔）</label>
            <input className={inputCls} placeholder="如：Java, 后端, 校招" value={postForm.tags}
              onChange={(e) => setPostForm({ ...postForm, tags: e.target.value })} />
          </div>
          <div>
            <label className="mb-1 block text-sm text-slate-600">附件（可选，图片 / Word / PDF）</label>
            <label className="flex cursor-pointer items-center justify-center rounded-lg border border-dashed border-slate-300 px-3 py-3 text-sm text-slate-500 transition hover:border-brand-400 hover:text-brand-600">
              <input
                type="file"
                className="hidden"
                accept=".jpg,.jpeg,.png,.gif,.webp,.doc,.docx,.pdf"
                multiple
                disabled={uploading}
                onChange={handleFileSelect}
              />
              {uploading ? "上传中…" : "点击选择文件"}
            </label>
            {postFiles.length > 0 && (
              <ul className="mt-2 space-y-1">
                {postFiles.map((f) => (
                  <li key={f.name} className="flex items-center gap-2 rounded-lg bg-slate-50 px-2 py-1.5 text-xs text-slate-600">
                    <span className="truncate">{f.original_name}</span>
                    <span className="shrink-0 text-slate-400">{(f.size / 1024).toFixed(0)}KB</span>
                    <button className="ml-auto shrink-0 text-rose-400 transition hover:text-rose-600" onClick={() => removeFile(f.name)}>
                      移除
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>
      </Modal>

      {/* 帖子详情 Modal */}
      <Modal
        open={!!detail}
        onClose={() => setDetail(null)}
        title={detail?.title || ""}
        footer={<button className={btnGhost} onClick={() => setDetail(null)}>关闭</button>}
      >
        {detail && (
          <div className="space-y-4">
            <div className="flex items-center gap-3 text-xs text-slate-400">
              <span className="flex items-center gap-1.5">
                <Avatar name={detail.author} src={detail.author_avatar} size={20} />
                <span className="text-slate-600">{detail.author}</span>
              </span>
              <Badge color="indigo">{detail.category}</Badge>
              <span>{formatDate(detail.created_at)}</span>
              {(isAdmin || detail.user_id === user?.id) && (
                <button
                  className="ml-auto text-rose-400 transition hover:text-rose-600"
                  onClick={() => removePost(detail.id, detail.title)}
                >
                  删除帖子
                </button>
              )}
            </div>
            <p className="whitespace-pre-wrap text-sm text-slate-700">{detail.content}</p>
            {detail.tags.length > 0 && (
              <div className="flex flex-wrap gap-1.5">
                {detail.tags.map((t) => (
                  <span key={t} className="rounded-full bg-slate-100 px-2 py-0.5 text-xs text-slate-500">#{t}</span>
                ))}
              </div>
            )}
            {detail.attachments.length > 0 && (
              <div className="space-y-2">
                {detail.attachments.filter((a) => a.kind === "image").map((a) => (
                  <img key={a.name} src={a.url} alt={a.original_name}
                    className="max-h-80 rounded-lg border border-slate-200" />
                ))}
                {detail.attachments.filter((a) => a.kind === "doc").map((a) => (
                  <a key={a.name} href={a.url} target="_blank" rel="noreferrer"
                    className="flex items-center gap-2 rounded-lg border border-slate-200 px-3 py-2 text-sm text-brand-600 transition hover:bg-brand-50">
                    <span>📄</span>
                    <span className="truncate">{a.original_name}</span>
                    <span className="ml-auto shrink-0 text-xs text-slate-400">{(a.size / 1024).toFixed(0)}KB</span>
                  </a>
                ))}
              </div>
            )}

            <div className="border-t border-slate-100 pt-4">
              <h4 className="mb-3 text-sm font-semibold text-slate-700">评论（{detail.comments.length}）</h4>
              <div className="space-y-3">
                {detail.comments.length === 0 && (
                  <p className="text-xs text-slate-400">还没有评论，来抢沙发</p>
                )}
                {detail.comments.map((c) => (
                  <div key={c.id} className="rounded-lg bg-slate-50 px-3 py-2">
                    <div className="flex items-center justify-between text-xs text-slate-400">
                      <span className="flex items-center gap-1.5">
                        <Avatar name={c.author} src={c.author_avatar} size={18} />
                        <span className="font-medium text-slate-600">{c.author}</span>
                      </span>
                      <span className="flex items-center gap-2">
                        <span>{formatDate(c.created_at)}</span>
                        {isAdmin && (
                          <button
                            className="text-rose-400 transition hover:text-rose-600"
                            onClick={() => removeComment(c.id)}
                          >
                            删除
                          </button>
                        )}
                      </span>
                    </div>
                    <p className="mt-1 text-sm text-slate-700">{c.content}</p>
                  </div>
                ))}
              </div>
              <div className="mt-3 flex gap-2">
                <input className={inputCls} placeholder="写下你的评论…" value={commentText}
                  onChange={(e) => setCommentText(e.target.value)}
                  onKeyDown={(e) => { if (e.key === "Enter") submitComment(); }} />
                <button className={btnPrimary} onClick={submitComment}>发送</button>
              </div>
            </div>
          </div>
        )}
      </Modal>

      {/* 提建议 Modal */}
      <Modal
        open={showFb}
        onClose={() => setShowFb(false)}
        title="提出建议"
        footer={
          <>
            <button className={btnGhost} onClick={() => setShowFb(false)}>取消</button>
            <button className={btnPrimary} onClick={submitFeedback} disabled={loading}>提交</button>
          </>
        }
      >
        <div className="space-y-3">
          <div>
            <label className="mb-1 block text-sm text-slate-600">分类</label>
            <select className={inputCls} value={fbForm.category}
              onChange={(e) => setFbForm({ ...fbForm, category: e.target.value })}>
              {FEEDBACK_CATEGORIES.map((c) => <option key={c} value={c}>{c}</option>)}
            </select>
          </div>
          <div>
            <label className="mb-1 block text-sm text-slate-600">标题</label>
            <input className={inputCls} placeholder="一句话概括你的建议" value={fbForm.title}
              onChange={(e) => setFbForm({ ...fbForm, title: e.target.value })} />
          </div>
          <div>
            <label className="mb-1 block text-sm text-slate-600">详细说明</label>
            <textarea className={`${inputCls} min-h-32`} placeholder="描述你希望改进的功能或遇到的问题…" value={fbForm.content}
              onChange={(e) => setFbForm({ ...fbForm, content: e.target.value })} />
          </div>
        </div>
      </Modal>

      {/* 处理建议（管理员） */}
      <Modal
        open={!!fbEditing}
        onClose={() => setFbEditing(null)}
        title="处理建议"
        footer={
          <>
            <button className={btnGhost} onClick={() => setFbEditing(null)}>取消</button>
            <button className={btnPrimary} onClick={saveFeedback} disabled={fbSaving}>保存</button>
          </>
        }
      >
        {fbEditing && (
          <div className="space-y-4">
            <div className="flex flex-wrap items-center gap-2 text-xs text-slate-400">
              <Badge color="indigo">{fbEditing.category}</Badge>
              <span>{fbEditing.author}</span>
              <span>{formatDate(fbEditing.created_at)}</span>
            </div>
            <div>
              <h3 className="text-base font-semibold text-slate-900">{fbEditing.title}</h3>
              <p className="mt-2 whitespace-pre-wrap text-sm leading-6 text-slate-600">{fbEditing.content}</p>
            </div>
            <div>
              <label className="mb-1 block text-sm text-slate-600">状态</label>
              <select className={inputCls} value={fbEditStatus} onChange={(e) => setFbEditStatus(e.target.value)}>
                {Object.entries(FEEDBACK_STATUS).map(([k, v]) => (
                  <option key={k} value={k}>{v.label}</option>
                ))}
              </select>
            </div>
            <div>
              <label className="mb-1 block text-sm text-slate-600">官方回复</label>
              <textarea className={`${inputCls} min-h-28`} placeholder="回复会公开显示在建议下方…"
                value={fbReplyText} onChange={(e) => setFbReplyText(e.target.value)} />
            </div>
          </div>
        )}
      </Modal>
    </div>
  );
};

export default Community;
