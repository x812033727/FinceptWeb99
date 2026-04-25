import { useVersion } from "@/hooks/useVersion";

export default function UpdateBadge() {
  const { data, isError } = useVersion();

  if (isError || !data || !data.update_available) return null;

  const url = data.html_url || "#";
  return (
    <a
      href={url}
      target="_blank"
      rel="noopener noreferrer"
      title={`Release notes — published ${data.published_at || "unknown"}`}
      className="px-2 py-0.5 rounded text-[11px] font-medium bg-amber-500/15 text-amber-600 dark:text-amber-400 border border-amber-500/30 hover:bg-amber-500/25 transition-colors"
    >
      v{data.latest} available
    </a>
  );
}
