on run
	-- 调用 启动系统.command（唯一启动入口）
	set appPath to POSIX path of (path to me)
	do shell script "
		APP_PATH=" & quoted form of appPath & "
		# 从 .app/Contents/MacOS/applet 或 .app 追溯到项目目录
		D=$(dirname \"$APP_PATH\")
		[[ \"$D\" == *Contents* ]] && D=$(dirname \"$D\") && D=$(dirname \"$D\") && D=$(dirname \"$D\")
		cd \"$D\" && bash ./启动系统.command
	"
end run
