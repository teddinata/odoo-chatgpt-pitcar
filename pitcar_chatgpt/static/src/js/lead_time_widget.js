odoo.define('pitcar_custom.lead_time_widget', function (require) {
  "use strict";

  var AbstractField = require('web.AbstractField');
  var registry = require('web.field_registry');

  var LeadTimeWidget = AbstractField.extend({
      template: 'LeadTimeWidgetTemplate',
      events: {
          'click .start-timer': '_onStartTimer',
          'click .stop-timer': '_onStopTimer',
          'click .reset-timer': '_onResetTimer',
      },

      _onStartTimer: function () {
          this._rpc({
              model: 'sale.order',
              method: 'action_record_time',
              args: [this.res_id, this.name],
          }).then(function () {
              this._render();
          }.bind(this));
      },

      _onStopTimer: function () {
          // Logic to stop timer
          this._render();
      },

      _onResetTimer: function () {
          this._rpc({
              model: 'sale.order',
              method: 'write',
              args: [[this.res_id], {[this.name]: false}],
          }).then(function () {
              this._render();
          }.bind(this));
      },

      _render: function () {
          this._super.apply(this, arguments);
          if (this.value) {
              this.$('.timer-value').text(this.value);
              this.$('.start-timer').hide();
              this.$('.stop-timer, .reset-timer').show();
          } else {
              this.$('.timer-value').text('00:00:00');
              this.$('.start-timer').show();
              this.$('.stop-timer, .reset-timer').hide();
          }
      },
  });

  registry.add('lead_time_widget', LeadTimeWidget);

  return LeadTimeWidget;
});