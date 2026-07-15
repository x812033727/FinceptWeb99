import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Mail, MessageCircle, RefreshCw, Send } from "lucide-react";
import { useTranslation } from "react-i18next";

import api from "@/lib/api";

type EventKind = "price_alert" | "strategy_health";
type ChannelKind = "email" | "line";

interface Channel {
  kind: ChannelKind;
  enabled: boolean;
  verified: boolean;
  configured: boolean;
  destination_hint: string | null;
  event_kinds: EventKind[];
  daily_digest: boolean;
  failed_count: number;
  last_success_at: string | null;
}

interface Binding {
  token: string;
  expires_at: string;
  instruction: string;
}

export default function NotificationChannels() {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const [binding, setBinding] = useState<Binding | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  const { data: channels = [], isLoading, refetch } = useQuery<Channel[]>({
    queryKey: ["notification-channels"],
    queryFn: () => api.get("/notifications/channels").then((r) => r.data),
  });

  const update = useMutation({
    mutationFn: ({ channel, enabled, eventKinds, dailyDigest }: {
      channel: Channel; enabled: boolean; eventKinds: EventKind[]; dailyDigest?: boolean;
    }) => api.put(`/notifications/channels/${channel.kind}`, {
      enabled, event_kinds: eventKinds,
      daily_digest: dailyDigest ?? channel.daily_digest,
    }),
    onSuccess: () => {
      setMessage(t("settings.notifications.saved"));
      void qc.invalidateQueries({ queryKey: ["notification-channels"] });
    },
    onError: () => setMessage(t("settings.notifications.channel_error")),
  });

  const bindLine = useMutation({
    mutationFn: () => api.post<Binding>("/notifications/channels/line/bind"),
    onSuccess: (response) => {
      setBinding(response.data);
      setMessage(null);
    },
    onError: () => setMessage(t("settings.notifications.channel_error")),
  });

  const remove = useMutation({
    mutationFn: (kind: ChannelKind) => api.delete(`/notifications/channels/${kind}`),
    onSuccess: () => {
      setBinding(null);
      setMessage(t("settings.notifications.disconnected"));
      void qc.invalidateQueries({ queryKey: ["notification-channels"] });
    },
    onError: () => setMessage(t("settings.notifications.channel_error")),
  });

  const test = useMutation({
    mutationFn: (kind: ChannelKind) => api.post(`/notifications/channels/${kind}/test`),
    onSuccess: () => setMessage(t("settings.notifications.test_sent")),
    onError: () => setMessage(t("settings.notifications.test_failed")),
  });

  if (isLoading) return <p className="text-xs text-muted-foreground">…</p>;

  function toggleEvent(channel: Channel, kind: EventKind) {
    const next = channel.event_kinds.includes(kind)
      ? channel.event_kinds.filter((item) => item !== kind)
      : [...channel.event_kinds, kind];
    if (next.length === 0) return;
    update.mutate({ channel, enabled: channel.enabled, eventKinds: next });
  }

  return (
    <div className="space-y-4 border-t border-border pt-4">
      <div>
        <p className="text-sm font-medium">{t("settings.notifications.channels")}</p>
        <p className="text-xs text-muted-foreground">{t("settings.notifications.channels_desc")}</p>
      </div>
      {channels.map((channel) => {
        const Icon = channel.kind === "email" ? Mail : MessageCircle;
        const ready = channel.configured && channel.verified;
        return (
          <div key={channel.kind} className="rounded border border-border p-3 space-y-3">
            <div className="flex items-start justify-between gap-3">
              <div className="flex items-start gap-2 min-w-0">
                <Icon className="h-4 w-4 mt-0.5 text-muted-foreground shrink-0" aria-hidden="true" />
                <div className="min-w-0">
                  <p className="text-sm font-medium">
                    {t(`settings.notifications.${channel.kind}`)}
                  </p>
                  <p className="text-xs text-muted-foreground break-all">
                    {!channel.configured
                      ? t("settings.notifications.provider_unconfigured")
                      : channel.kind === "line" && channel.verified
                        ? t("settings.notifications.connected")
                        : channel.destination_hint ?? t("settings.notifications.not_connected")}
                  </p>
                </div>
              </div>
              <button
                type="button"
                disabled={!ready || update.isPending}
                aria-pressed={channel.enabled}
                onClick={() => update.mutate({
                  channel, enabled: !channel.enabled, eventKinds: channel.event_kinds,
                })}
                className="px-3 py-1.5 rounded border border-border text-xs min-h-[36px] disabled:opacity-40 shrink-0"
              >
                {channel.enabled
                  ? t("settings.notifications.disable")
                  : t("settings.notifications.enable")}
              </button>
            </div>

            <div className="flex flex-wrap gap-x-4 gap-y-2 text-xs">
              {(["price_alert", "strategy_health"] as EventKind[]).map((kind) => (
                <label key={kind} className="inline-flex items-center gap-1.5">
                  <input
                    type="checkbox"
                    checked={channel.event_kinds.includes(kind)}
                    disabled={!channel.verified || update.isPending}
                    onChange={() => toggleEvent(channel, kind)}
                  />
                  {t(`settings.notifications.event_${kind}`)}
                </label>
              ))}
              {channel.kind === "email" && (
                <label className="inline-flex items-center gap-1.5">
                  <input
                    type="checkbox"
                    checked={channel.daily_digest}
                    disabled={!channel.configured || update.isPending}
                    onChange={() => update.mutate({
                      channel,
                      enabled: channel.enabled,
                      eventKinds: channel.event_kinds,
                      dailyDigest: !channel.daily_digest,
                    })}
                  />
                  {t("settings.notifications.daily_digest")}
                </label>
              )}
            </div>

            <div className="flex flex-wrap gap-2">
              {channel.kind === "line" && channel.configured && !channel.verified && !binding && (
                <button
                  type="button"
                  disabled={bindLine.isPending}
                  onClick={() => bindLine.mutate()}
                  className="px-3 py-1.5 rounded border border-border text-xs min-h-[36px]"
                >
                  {t("settings.notifications.connect_line")}
                </button>
              )}
              {channel.verified && (
                <button
                  type="button"
                  disabled={!channel.configured || test.isPending}
                  onClick={() => test.mutate(channel.kind)}
                  className="px-3 py-1.5 rounded border border-border text-xs min-h-[36px] inline-flex items-center gap-1.5"
                >
                  <Send className="h-3.5 w-3.5" aria-hidden="true" />
                  {t("settings.notifications.send_test")}
                </button>
              )}
              {channel.kind === "line" && channel.verified && (
                <>
                  <button
                    type="button"
                    onClick={() => void refetch()}
                    className="px-3 py-1.5 rounded border border-border text-xs min-h-[36px] inline-flex items-center gap-1.5"
                  >
                    <RefreshCw className="h-3.5 w-3.5" aria-hidden="true" />
                    {t("settings.notifications.refresh")}
                  </button>
                  <button
                    type="button"
                    disabled={remove.isPending}
                    onClick={() => remove.mutate("line")}
                    className="px-3 py-1.5 rounded border border-danger/40 text-danger text-xs min-h-[36px]"
                  >
                    {t("settings.notifications.disconnect")}
                  </button>
                </>
              )}
            </div>
          </div>
        );
      })}
      {binding && (
        <div className="rounded border border-primary/30 bg-primary/5 p-3 space-y-1">
          <p className="text-xs font-medium">{t("settings.notifications.line_binding_title")}</p>
          <code className="block text-xs break-all select-all">FINCEPT {binding.token}</code>
          <p className="text-xs text-muted-foreground">{t("settings.notifications.line_binding_desc")}</p>
          <button
            type="button"
            onClick={() => void refetch()}
            className="mt-2 px-3 py-1.5 rounded border border-border text-xs min-h-[36px] inline-flex items-center gap-1.5"
          >
            <RefreshCw className="h-3.5 w-3.5" aria-hidden="true" />
            {t("settings.notifications.refresh")}
          </button>
        </div>
      )}
      {message && <p role="status" className="text-xs text-muted-foreground">{message}</p>}
    </div>
  );
}
