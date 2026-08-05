import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import "./globals.css";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  metadataBase: new URL(process.env.NEXT_PUBLIC_SITE_URL ?? "http://localhost:3000"),
  title: "ForgeFlow · AI 开发平台",
  description: "由四个专业 Agent 驱动的多项目、多仓库软件交付平台。",
  icons: {
    icon: "/favicon.svg",
    shortcut: "/favicon.svg",
  },
  openGraph: {
    title: "ForgeFlow · AI 开发平台",
    description: "四个 Agent，一条可信的软件交付链路",
    images: [{ url: "/og.png", width: 1536, height: 1024, alt: "ForgeFlow 四 Agent 软件交付链路" }],
  },
  twitter: {
    card: "summary_large_image",
    title: "ForgeFlow · AI 开发平台",
    description: "四个 Agent，一条可信的软件交付链路",
    images: ["/og.png"],
  },
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="zh-CN">
      <body
        className={`${geistSans.variable} ${geistMono.variable} antialiased`}
      >
        {children}
      </body>
    </html>
  );
}
