import { cn } from "@/lib/utils";
import { getPersonaIdentity } from "./personaIdentity";

interface PersonaAvatarProps {
  personaId: string;
  /** Localised display name — used to derive the avatar initial when
   *  no explicit short label is provided via i18n. */
  name: string;
  /** Explicit 1-2 char glyph; overrides derivation from `name`. */
  initial?: string;
  /** sm = 24px (mobile dense); md = 28px (default desktop bubble). */
  size?: "sm" | "md";
  className?: string;
}

// Round persona avatar — coloured by per-id HSL hue, displays a
// short initial. Same persona renders identically everywhere so
// readers can scan a multi-round transcript without re-reading
// names.
export function PersonaAvatar({
  personaId, name, initial, size = "md", className,
}: PersonaAvatarProps) {
  const identity = getPersonaIdentity(personaId, name, initial);
  const dim = size === "sm" ? "h-6 w-6 text-[10px]" : "h-7 w-7 text-[11px]";
  return (
    <span
      className={cn(
        "inline-flex items-center justify-center rounded-full font-semibold tracking-tight ring-1 shrink-0 select-none",
        dim,
        className,
      )}
      style={{
        backgroundColor: identity.softColor,
        color: identity.accentColor,
        // Tailwind ring uses --tw-ring-color; setting it inline keeps
        // the per-persona hue without bloating the class list.
        "--tw-ring-color": identity.ringColor,
      } as React.CSSProperties}
      aria-label={name}
      title={name}
    >
      {identity.initial}
    </span>
  );
}
