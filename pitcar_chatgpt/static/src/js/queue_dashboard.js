/** @odoo-module **/
import { registry } from "@web/core/registry";
import { kanbanView } from "@web/views/kanban/kanban_view";
import { KanbanController } from "@web/views/kanban/kanban_controller";
import { useService } from "@web/core/utils/hooks";

const debugService = {
  dependencies: ["bus_service"],
  start(env) {
      env.services.bus_service.addEventListener("notification", (notification) => {
          console.group("Queue Debug");
          console.log("Notification received:", notification);
          console.log("Timestamp:", new Date().toISOString());
          console.groupEnd();
      });
  },
};

registry.category("services").add("queue_debug", debugService);


class QueueKanbanController extends KanbanController {
    setup() {
        super.setup();
        
        // Get required services
        this.busService = useService("bus_service");
        console.log("Bus Service initialized");

        this.orm = useService("orm");
        
        // Subscribe to bus channel
        this.busService.addChannel("queue_dashboard");
        console.log("Queue dashboard channel added");
        this.busService.addEventListener("notification", (notification) => {
          console.group("Queue Controller Notification");
          console.log("Raw notification:", notification);
          console.log("Current time:", new Date().toISOString());
          console.groupEnd();
          
          this._onNotification(notification);
      });
        
        // Set up auto refresh every 30 seconds as fallback
        this.intervalId = setInterval(() => {
            console.log("Auto refresh triggered");
            this._refreshView();
        }, 30000);
    }

    /**
     * Handle bus notifications
     * @param {Object} notification - The notification object
     */
    async _onNotification(notification) {
        if (!notification) return;
        
        console.log('Received notification:', notification); // Debug log
        
        try {
            // Handle single notification object
            if (notification.type === "refresh_dashboard") {
                await this._refreshView();
                return;
            }
            
            // Handle notification array/channel format
            if (Array.isArray(notification)) {
                for (const [channel, message] of notification) {
                    if (channel === 'queue_dashboard' && message.type === 'refresh_dashboard') {
                        await this._refreshView();
                        break;
                    }
                }
            }
        } catch (error) {
            console.error('Error handling notification:', error);
        }
    }

    async _refreshView() {
        try {
            await this.model.root.load();
            this.render(true);
            
            // Update last refresh indicator jika ada
            const summaryCard = this.el.querySelector('.summary-card .text-muted');
            if (summaryCard) {
                const currentTime = new Date().toLocaleTimeString('id-ID', {
                    timeZone: 'Asia/Jakarta',
                    hour: '2-digit',
                    minute: '2-digit',
                    second: '2-digit'
                });
            }
        } catch (error) {
            console.error('Failed to refresh dashboard:', error);
        }
    }    

    /**
     * @override
     */
    destroy() {
        super.destroy();
        if (this.intervalId) {
            clearInterval(this.intervalId);
        }
        this.busService.removeEventListener("notification", this._onNotification.bind(this));
    }
}

// Register the custom view
registry.category("views").add("queue_metric_kanban", {
    ...kanbanView,
    Controller: QueueKanbanController,
});