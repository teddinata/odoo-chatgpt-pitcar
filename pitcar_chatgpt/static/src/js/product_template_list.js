odoo.define('pitcar_custom.product_template_list', function (require) {
    "use strict";
  
    var ListController = require('web.ListController');
    var ListView = require('web.ListView');
    var viewRegistry = require('web.view_registry');
  
    var ProductTemplateListController = ListController.extend({
        renderButtons: function () {
            this._super.apply(this, arguments);
            if (this.$buttons) {
                var $updateButton = $('<button>', {
                    text: 'UPDATE GUDANG',
                    class: 'btn btn-info o_list_button_update',
                    click: this.proxy('updateInventory')
                });
                this.$buttons.find('.o_list_button_add').after($updateButton);
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
  
    var ProductTemplateListView = ListView.extend({
        config: _.extend({}, ListView.prototype.config, {
            Controller: ProductTemplateListController,
        }),
    });
  
    viewRegistry.add('product_template_list', ProductTemplateListView);
  
    return ProductTemplateListController;
  });