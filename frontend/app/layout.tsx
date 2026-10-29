import type { Metadata } from "next";
import "./styles.css";


export const metadata: Metadata = {
  title: "Highest Temperature in Miami",
  description: "Kalshi-style Miami daily temperature market clone"
};


export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
