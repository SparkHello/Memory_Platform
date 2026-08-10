import { defineConfig, devices } from "@playwright/test";

const executablePath = process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH;

export default defineConfig({
  testDir: "./e2e",
  outputDir: "./test-results",
  fullyParallel: false,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 1 : 0,
  // The three projects share one synthetic Vite origin (including the
  // non-secure LAN hostname used for clipboard fallback).  Run them serially
  // everywhere so a developer machine's local HTTP proxy cannot race or
  // transiently return 503 for that mapped hostname.
  workers: 1,
  reporter: "line",
  use: {
    baseURL: "http://127.0.0.1:4173",
    browserName: "chromium",
    reducedMotion: "reduce",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "off",
    launchOptions: {
      ...(executablePath ? { executablePath } : {}),
      args: [
        "--host-resolver-rules=MAP memory-platform.test 127.0.0.1,EXCLUDE localhost",
        "--no-proxy-server",
        "--proxy-server=direct://",
        "--proxy-bypass-list=*"
      ]
    }
  },
  projects: [
    {
      name: "desktop-1440x900",
      testMatch: /desktop\.setup\.spec\.ts/,
      use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 900 } }
    },
    {
      name: "mobile-390x844",
      testMatch: /mobile-390\.navigation\.spec\.ts/,
      use: {
        ...devices["Pixel 7"],
        viewport: { width: 390, height: 844 },
        screen: { width: 390, height: 844 }
      }
    },
    {
      name: "mobile-375x667",
      testMatch: /mobile-375\.safety\.spec\.ts/,
      use: {
        ...devices["Pixel 7"],
        viewport: { width: 375, height: 667 },
        screen: { width: 375, height: 667 }
      }
    }
  ],
  webServer: {
    command: "npm run dev:e2e",
    url: "http://127.0.0.1:4173/ui/",
    reuseExistingServer: !process.env.CI,
    timeout: 30_000
  }
});
