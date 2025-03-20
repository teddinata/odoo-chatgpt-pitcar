odoo.define('pitcar_custom.product_template_kanban', function (require) {
  "use strict";

  var KanbanController = require('web.KanbanController');
  var KanbanView = require('web.KanbanView');
  var viewRegistry = require('web.view_registry');

  var ProductTemplateKanbanController = KanbanController.extend({
      renderButtons: function () {
          this._super.apply(this, arguments);
          if (this.$buttons) {
              var $updateButton = $('<button>', {
                  text: 'UPDATE GUDANG',
                  class: 'btn btn-info o_kanban_button_update',
                  click: this.proxy('updateInventory')
              });
              this.$buttons.find('.o-kanban-button-new').after($updateButton);
          }
      },
      updateInventory: function () {
          this._rpc({
              model: 'product.template',
              method: 'action_update_all_inventory_age',
              args: [],
          }).then(function () {
              this.reload();
          }.bind(this));
      },
  });

  var ProductTemplateKanbanView = KanbanView.extend({
      config: _.extend({}, KanbanView.prototype.config, {
          Controller: ProductTemplateKanbanController,
      }),
  });

  viewRegistry.add('product_template_kanban', ProductTemplateKanbanView);
});