import type { Metadata } from "next";
import Link from "next/link";
import "./globals.css";

export const metadata: Metadata = {
  title: "Coloring Studio",
  description: "AI coloring book publishing studio for Amazon KDP",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="min-h-screen font-sans antialiased">
        <a href="#main" className="sr-only focus:not-sr-only focus:absolute focus:left-2 focus:top-2 focus:rounded focus:bg-white focus:p-2">Skip to content</a>
        <header className="sticky top-0 z-30 border-b border-slate-200 bg-white/90 backdrop-blur">
          <div className="mx-auto flex max-w-[1500px] items-center gap-4 px-4 py-2">
            <Link href="/" className="flex items-center gap-2 text-lg font-bold text-ink">
              <span aria-hidden className="grid h-8 w-8 place-items-center rounded-lg bg-brand-600 text-white">✎</span>
              Coloring Studio
            </Link>
            <nav className="ml-auto flex gap-1 text-sm" aria-label="Main">
              <Link className="rounded-lg px-3 py-1.5 hover:bg-slate-100" href="/">Library</Link>
              <Link className="rounded-lg px-3 py-1.5 hover:bg-slate-100" href="/new">New book</Link>
              <Link className="rounded-lg px-3 py-1.5 hover:bg-slate-100" href="/providers">AI Model Control Center</Link>
            </nav>
          </div>
        </header>
        <main id="main" className="mx-auto max-w-[1500px] px-4 py-6">{children}</main>
      </body>
    </html>
  );
}
