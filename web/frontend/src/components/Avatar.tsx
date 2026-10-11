import type { FC } from "react";

/**
 * 用户头像：有头像图片显示图片，否则显示昵称首字母圆形。
 */
const Avatar: FC<{ name: string; src?: string | null; size?: number }> = ({
  name,
  src,
  size = 32,
}) => {
  const style = { width: size, height: size };
  if (src) {
    return (
      <img
        src={src}
        alt={name}
        style={style}
        className="flex-shrink-0 rounded-full object-cover"
      />
    );
  }
  return (
    <div
      style={{ ...style, fontSize: Math.round(size * 0.42) }}
      className="flex flex-shrink-0 items-center justify-center rounded-full bg-slate-200 font-semibold text-slate-500"
    >
      {name?.charAt(0).toUpperCase() || "U"}
    </div>
  );
};

export default Avatar;
