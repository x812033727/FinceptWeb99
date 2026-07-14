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
      className="hidden sm:inline-block px-2 py-0.5 rounded text-meta font-medium bg-warning/15 text-warning border border-warning/30 hover:bg-warning/25 transition-colors"
    >
      v{data.latest} available
    </a>
  );
}
