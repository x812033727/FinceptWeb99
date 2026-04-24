import React from "react";

interface Props {
  children: React.ReactNode;
  fallback?: React.ReactNode;
}

interface State {
  hasError: boolean;
  error: Error | null;
}

export class ErrorBoundary extends React.Component<Props, State> {
  state: State = { hasError: false, error: null };

  static getDerivedStateFromError(error: Error): State {
    return { hasError: true, error };
  }

  render() {
    if (this.state.hasError) {
      if (this.props.fallback) return this.props.fallback;
      return (
        <div className="p-8 flex flex-col items-center gap-3 text-center">
          <p className="text-red-400 font-medium">Something went wrong.</p>
          <p className="text-xs text-muted-foreground max-w-sm break-words">
            {this.state.error?.message ?? "An unexpected error occurred."}
          </p>
          <button
            className="mt-1 px-4 py-1.5 text-xs rounded bg-primary text-primary-foreground"
            onClick={() => this.setState({ hasError: false, error: null })}
          >
            Try again
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}
