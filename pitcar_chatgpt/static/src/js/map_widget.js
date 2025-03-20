/** @odoo-module **/

import { registry } from "@web/core/registry";
import { standardFieldProps } from "@web/views/fields/standard_field_props";
import { Component, onMounted, onWillUnmount, useRef, xml } from "@odoo/owl";

export class MapWidget extends Component {
    static template = xml`
        <div class="o_map_widget h-100">
            <div t-ref="mapRef" style="height: 500px;"/>
        </div>
    `;
    static props = {
        ...standardFieldProps,
        record: Object,
    };

    setup() {
        this.mapRef = useRef('mapRef');
        this.map = null;
        this.marker = null;
        this.circle = null;

        onMounted(() => this.initMap());
        onWillUnmount(() => {
            if (this.map) {
                this.map.remove();
            }
        });
    }

    initMap() {
        if (typeof L === 'undefined') {
            console.error('Leaflet is not loaded');
            return;
        }

        const record = this.props.record;
        const lat = record.data.latitude || -6.3089;
        const lng = record.data.longitude || 106.8456;
        const radius = record.data.radius || 100;

        this.map = L.map(this.mapRef.el).setView([lat, lng], 15);

        L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
            attribution: '© OpenStreetMap contributors'
        }).addTo(this.map);

        this.marker = L.marker([lat, lng], {
            draggable: true
        }).addTo(this.map);

        this.circle = L.circle([lat, lng], {
            radius: radius,
            color: 'red',
            fillColor: '#f03',
            fillOpacity: 0.2
        }).addTo(this.map);

        this.marker.on('dragend', (e) => {
            const pos = e.target.getLatLng();
            record.update({
                latitude: pos.lat,
                longitude: pos.lng
            });
            this.circle.setLatLng(pos);
        });

        record.addEventListener('update', () => {
            const newLat = record.data.latitude;
            const newLng = record.data.longitude;
            const newRadius = record.data.radius;

            if (newLat && newLng) {
                this.marker.setLatLng([newLat, newLng]);
                this.map.setView([newLat, newLng]);
                this.circle.setLatLng([newLat, newLng]);
            }
            if (newRadius) {
                this.circle.setRadius(newRadius);
            }
        });
    }
}

registry.category("fields").add("map_widget", MapWidget);