/**
 * Skeleton primitive — a pulsing placeholder block in the muted-foreground
 * tone. Use this to mock the rough shape of content that's still loading
 * so users perceive the page as already arriving instead of staring at
 * a "Loading…" string.
 *
 * Width / height come from the caller via Tailwind utility classes; the
 * primitive only controls the colour, rounding, and the pulse animation.
 */
interface SkeletonProps {
  className?: string;
}

export function Skeleton({ className = "" }: SkeletonProps) {
  return (
    <div
      aria-hidden="true"
      className={`animate-pulse bg-muted/60 rounded ${className}`}
    />
  );
}

/**
 * Page-shaped skeleton used as the Suspense fallback for every lazy
 * route. It mirrors the common layout shared by most pages — outer
 * `p-4 sm:p-6` padding, an h1-sized title bar, a subtitle line, and a
 * couple of full-width content blocks — so the visual hand-off when
 * the real page mounts is barely noticeable.
 */
export function PageSkeleton() {
  return (
    <div className="p-4 sm:p-6 space-y-5 sm:space-y-6">
      <div className="space-y-2">
        <Skeleton className="h-7 sm:h-8 w-40" />
        <Skeleton className="h-3 w-64 max-w-full" />
      </div>
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        <Skeleton className="h-20" />
        <Skeleton className="h-20" />
        <Skeleton className="h-20" />
        <Skeleton className="h-20" />
      </div>
      <Skeleton className="h-64" />
    </div>
  );
}
