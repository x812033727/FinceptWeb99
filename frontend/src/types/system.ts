export interface VersionStatus {
  current: string;
  latest: string;
  update_available: boolean;
  html_url: string;
  published_at: string;
}

export type UpdateStatus = "started" | "not_configured" | "failed";

export interface UpdateResult {
  status: UpdateStatus;
  message: string;
}
