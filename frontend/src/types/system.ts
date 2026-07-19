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

export type DeployPhase =
  | "idle"
  | "starting"
  | "backing_up"
  | "pulling"
  | "building"
  | "pausing"
  | "reset_stale"
  | "migrating"
  | "restarting"
  | "nginx"
  | "verifying"
  | "completed"
  | "failed"
  | "unknown";

export interface DeployTriggerResult {
  status: "started" | "failed";
  trigger_id: string;
  message: string;
}

export interface DeployStatus {
  phase: DeployPhase;
  started_at?: string | null;
  finished_at?: string | null;
  before_sha?: string | null;
  after_sha?: string | null;
  branch?: string | null;
  actor?: string | null;
  trigger_id?: string | null;
  error?: string | null;
  log_tail?: string[] | null;
}
