odoo.define('pitcar_custom.timeline', function (require) {
  'use strict';

  var FormRenderer = require('web.FormRenderer');
  var core = require('web.core');
  var QWeb = core.qweb;

  FormRenderer.include({
      events: _.extend({}, FormRenderer.prototype.events, {
          'mouseenter .timeline .event': '_onEventMouseEnter',
          'mouseleave .timeline .event': '_onEventMouseLeave'
      }),

      _updateTimeline: function() {
          var self = this;
          var $timeline = this.$('.timeline');
          if (!$timeline.length) return;

          // Get timeline bounds
          var startTime = moment(this.state.data.controller_mulai_servis);
          var endTime = moment(this.state.data.controller_selesai);
          if (!startTime || !endTime) return;

          // Update events position
          $timeline.find('.event[data-time]').each(function() {
              var $event = $(this);
              var eventTime = moment($event.data('time'));
              if (eventTime) {
                  var position = self._computeEventPosition(startTime, endTime, eventTime);
                  $event.css('left', position + '%');
              }
          });

          // Update progress bar
          var progress = this.state.data.lead_time_progress || 0;
          $timeline.find('.progress').css('width', progress + '%');
      },

      _computeEventPosition: function(startTime, endTime, eventTime) {
          if (!startTime || !endTime || !eventTime) return 0;

          var totalDuration = endTime.diff(startTime);
          var eventDuration = eventTime.diff(startTime);
          
          return Math.max(0, Math.min(100, (eventDuration / totalDuration) * 100));
      },

      _onEventMouseEnter: function(ev) {
          var $event = $(ev.currentTarget);
          $event.find('.event-details').removeClass('d-none');
      },

      _onEventMouseLeave: function(ev) {
          var $event = $(ev.currentTarget);
          $event.find('.event-details').addClass('d-none');
      },

      _render: function() {
          var self = this;
          return this._super.apply(this, arguments).then(function() {
              self._updateTimeline();
          });
      }
  });
});