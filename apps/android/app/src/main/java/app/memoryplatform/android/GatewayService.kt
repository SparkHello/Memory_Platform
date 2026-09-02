package app.memoryplatform.android

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import android.util.Log
import com.chaquo.python.Python
import java.io.File
import kotlin.concurrent.thread

/**
 * Foreground service that hosts both gateways inside the embedded Python runtime.
 * All Python calls run on a worker thread; Chaquopy executes them on the calling
 * JVM thread, and start() blocks until both uvicorn servers are listening.
 */
class GatewayService : Service() {

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_STOP) {
            stopStack()
            stopSelf()
            return START_NOT_STICKY
        }
        startInForeground(getString(R.string.state_starting))
        if (StackState.state() == "running" || !startInFlight.compareAndSet(false, true)) return START_STICKY
        StackState.update("""{"state":"starting"}""")
        thread(name = "memory-platform-start") {
            try {
                val ui = ConsoleAssets.ensureExtracted(this)
                val dataDir = File(filesDir, "memory-platform").apply { mkdirs() }
                val status = python().callAttr(
                    "start", dataDir.absolutePath, StackState.MEMORY_PORT, StackState.MODEL_PORT, ui.absolutePath,
                ).toString()
                StackState.update(status)
                val running = StackState.state() == "running"
                updateNotification(getString(if (running) R.string.state_running else R.string.state_failed))
                if (!running) Log.e(TAG, "stack failed: $status")
            } catch (e: Exception) {
                Log.e(TAG, "start failed", e)
                StackState.update("""{"state":"failed","error":${quote(e.toString())}}""")
                updateNotification(getString(R.string.state_failed))
            } finally {
                startInFlight.set(false)
            }
        }
        return START_STICKY
    }

    override fun onDestroy() {
        stopStack()
        super.onDestroy()
    }

    private fun stopStack() {
        try {
            StackState.update(python().callAttr("stop").toString())
        } catch (e: Exception) {
            Log.w(TAG, "stop failed", e)
        }
    }

    private fun python() = Python.getInstance().getModule("embedded_stack")

    private fun startInForeground(text: String) {
        val manager = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            manager.createNotificationChannel(
                NotificationChannel(CHANNEL, getString(R.string.notification_channel), NotificationManager.IMPORTANCE_LOW),
            )
        }
        val notification = buildNotification(text)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
            startForeground(NOTIFICATION_ID, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE)
        } else {
            startForeground(NOTIFICATION_ID, notification)
        }
    }

    private fun updateNotification(text: String) {
        val manager = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        manager.notify(NOTIFICATION_ID, buildNotification(text))
    }

    private fun buildNotification(text: String): Notification {
        val open = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java), PendingIntent.FLAG_IMMUTABLE,
        )
        val stop = PendingIntent.getService(
            this, 1, Intent(this, GatewayService::class.java).setAction(ACTION_STOP), PendingIntent.FLAG_IMMUTABLE,
        )
        return Notification.Builder(this, CHANNEL)
            .setSmallIcon(android.R.drawable.stat_notify_sync_noanim)
            .setContentTitle(getString(R.string.notification_title))
            .setContentText(text)
            .setContentIntent(open)
            .addAction(Notification.Action.Builder(null, getString(R.string.action_stop), stop).build())
            .setOngoing(true)
            .build()
    }

    companion object {
        private val startInFlight = java.util.concurrent.atomic.AtomicBoolean(false)
        const val ACTION_STOP = "app.memoryplatform.android.STOP"
        private const val TAG = "GatewayService"
        private const val CHANNEL = "gateway"
        private const val NOTIFICATION_ID = 1

        fun start(context: Context) {
            context.startForegroundService(Intent(context, GatewayService::class.java))
        }

        fun stop(context: Context) {
            context.startService(Intent(context, GatewayService::class.java).setAction(ACTION_STOP))
        }

        private fun quote(value: String) = "\"" + value.replace("\\", "\\\\").replace("\"", "\\\"") + "\""
    }
}
