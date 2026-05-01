import { useTranslation } from "react-i18next";
import { useQuery } from "@tanstack/react-query";
import api from "@/lib/api";

interface NewsItem {
  title: string;
  publisher: string;
  link: string;
  published_at: string;
  thumbnail: string | null;
}

export function NewsFeed({ symbol, market }: { symbol: string; market: "US" | "TW" | "CRYPTO" }) {
  const { t } = useTranslation();
  const prefix = market === "US" ? "us" : market === "CRYPTO" ? "crypto" : "tw";
  const { data: items = [], isLoading } = useQuery<NewsItem[]>({
    queryKey: ["news", market, symbol],
    queryFn: () => api.get(`/${prefix}/news/${symbol}`).then((r) => r.data),
    staleTime: 5 * 60_000,
  });

  if (isLoading) {
    return (
      <div className="bg-card border border-border rounded-lg p-4 text-xs text-muted-foreground animate-pulse">
        {t("common.loading")}
      </div>
    );
  }
  if (!items.length) {
    return (
      <div className="bg-card border border-border rounded-lg p-4 text-xs text-muted-foreground">
        No recent news found.
      </div>
    );
  }

  return (
    <div className="bg-card border border-border rounded-lg overflow-hidden divide-y divide-border">
      {items.map((item, i) => (
        <a
          key={i}
          href={item.link}
          target="_blank"
          rel="noopener noreferrer"
          className="flex gap-3 p-3 hover:bg-accent/5 transition-colors"
        >
          {item.thumbnail && (
            <img
              src={item.thumbnail}
              alt=""
              className="w-16 h-12 object-cover rounded shrink-0"
              onError={(e) => { (e.target as HTMLImageElement).style.display = "none"; }}
            />
          )}
          <div className="flex-1 min-w-0 space-y-1">
            <p className="text-sm font-medium leading-snug line-clamp-2">{item.title}</p>
            <p className="text-xs text-muted-foreground">
              {item.publisher} ·{" "}
              {new Date(item.published_at).toLocaleDateString(undefined, {
                month: "short",
                day: "numeric",
                year: "numeric",
              })}
            </p>
          </div>
        </a>
      ))}
    </div>
  );
}
