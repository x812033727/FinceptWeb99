import type { Dispatch, SetStateAction } from "react";

import type { IssuedKeyResponse } from "./types";

/**
 * Amber "copy this once" banner shown after a key is issued. Pure
 * display: the plaintext + dismissal live in the entry card's
 * `issuedKey` state (auto-cleared on a timer there). The original
 * parent-level `{issuedKey && …}` guard becomes a `? … : null`
 * return so TS narrows `issuedKey` to non-null inside the JSX.
 */
export function IssuedKeyBanner({
  issuedKey,
  setIssuedKey,
}: {
  issuedKey: IssuedKeyResponse | null;
  setIssuedKey: Dispatch<SetStateAction<IssuedKeyResponse | null>>;
}) {
  return issuedKey ? (
    <div className="mt-3 rounded border border-warning/40 bg-warning/10 p-3 text-xs">
      <div className="mb-1 font-semibold text-warning">
        ⚠ Copy this key now — it will not be shown again.
      </div>
      <div className="break-all font-mono">
        {issuedKey.plaintext}
      </div>
      <div className="mt-2 flex items-center gap-2">
        <button
          type="button"
          onClick={() => {
            navigator.clipboard?.writeText(issuedKey.plaintext);
          }}
          className="rounded border border-border bg-background px-2 py-0.5 text-meta"
        >
          Copy
        </button>
        <button
          type="button"
          onClick={() => setIssuedKey(null)}
          className="rounded border border-border bg-background px-2 py-0.5 text-meta"
        >
          Dismiss
        </button>
        <span className="text-muted-foreground">
          Issued for {issuedKey.owner_email} · prefix{" "}
          <code>{issuedKey.prefix}</code> ·{" "}
          {issuedKey.plan_code ? (
            <>
              plan <code>{issuedKey.plan_code}</code>
            </>
          ) : (
            <>plan free-tier</>
          )}
        </span>
      </div>
    </div>
  ) : null;
}
