import React from "react";
import i18n from "i18next";

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

  reset = () => this.setState({ hasError: false, error: null });
  reload = () => window.location.reload();

  render() {
    if (this.state.hasError) {
      if (this.props.fallback) return this.props.fallback;
      // i18n.t() called directly (not via hook) so the class component
      // can stay a class. Translations load eagerly on app boot.
      return (
        <div className="p-6 sm:p-8 flex flex-col items-center gap-3 text-center min-h-[40vh] justify-center">
          <div
            className="w-12 h-12 rounded-full bg-red-500/10 border border-red-500/30 flex items-center justify-center text-red-400 text-2xl font-semibold"
            aria-hidden="true"
          >
            !
          </div>
          <p className="text-foreground font-medium text-base">
            {i18n.t("common.something_wrong")}
          </p>
          <p className="text-xs text-muted-foreground max-w-sm break-words">
            {this.state.error?.message ?? i18n.t("common.unexpected_error")}
          </p>
          <div className="flex gap-2 mt-2">
            <button
              className="px-4 py-1.5 text-xs rounded bg-primary text-primary-foreground hover:opacity-90 transition-opacity"
              onClick={this.reset}
            >
              {i18n.t("common.try_again")}
            </button>
            <button
              className="px-4 py-1.5 text-xs rounded border border-border text-muted-foreground hover:text-foreground hover:border-foreground/40 transition-colors"
              onClick={this.reload}
            >
              {i18n.t("common.reload_page")}
            </button>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}
