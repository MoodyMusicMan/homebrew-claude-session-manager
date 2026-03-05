class ClaudeSessionManager < Formula
  desc "Floating GUI showing all active Claude Code sessions"
  homepage "https://github.com/MoodyMusicMan/homebrew-claude-session-manager"
  url "https://github.com/MoodyMusicMan/homebrew-claude-session-manager/archive/refs/tags/v0.1.0.tar.gz"
  sha256 "PLACEHOLDER"
  license "MIT"

  depends_on "python@3.12"
  depends_on "python-tk@3.12"
  depends_on :macos

  def install
    libexec.install "scripts/session-tracker.py"
    bin.install "scripts/session-ctl.sh" => "session-ctl"

    (bin/"claude-session-manager").write <<~EOS
      #!/bin/bash
      exec "#{Formula["python@3.12"].opt_bin}/python3.12" "#{libexec}/session-tracker.py" "$@"
    EOS
  end

  def caveats
    <<~EOS
      For menu bar integration (recommended), install PyObjC:
        #{Formula["python@3.12"].opt_bin}/pip3.12 install pyobjc-framework-Cocoa

      Start the session manager:
        claude-session-manager &

      Or run as a background service:
        brew services start claude-session-manager

      Control the GUI from the terminal:
        session-ctl screenshot
        session-ctl state
        session-ctl refresh
    EOS
  end

  service do
    run [bin/"claude-session-manager"]
    keep_alive true
    log_path var/"log/claude-session-manager.log"
    error_log_path var/"log/claude-session-manager.log"
  end

  test do
    assert_match "Session Tracker", (libexec/"session-tracker.py").read
    assert_match "session-ctl", (bin/"session-ctl").read
  end
end
