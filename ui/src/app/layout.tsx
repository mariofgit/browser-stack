import "./globals.css";
import type { ReactNode } from "react";

export const metadata = {
  title: "Finance Agent — Neuforce",
  description: "Morning shot (WSJ y NYT) y resumen de noticias para mercados financieros",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
