import "./globals.css";
import type { ReactNode } from "react";

export const metadata = {
  title: "WSJ Morning Shot",
  description: "WSJ morning shot via Browserbase scraping",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
