/** @odoo-module **/

import { registry } from "@web/core/registry";
import { Component, useState, useRef } from "@odoo/owl";
import { useInputField } from "@web/views/fields/input_field_hook";
import { standardFieldProps } from "@web/views/fields/standard_field_props";

export class PasswordToggleField extends Component {
    static template = "distributed_archive_storage.PasswordToggleField";
    static props = { ...standardFieldProps };

    setup() {
        this.state = useState({ visible: false });
        useInputField({
            getValue: () => this.props.record.data[this.props.name] || "",
            refName: "input",
        });
    }

    toggleVisibility() {
        this.state.visible = !this.state.visible;
    }

    get inputType() {
        return this.state.visible ? "text" : "password";
    }
}

export const passwordToggleField = {
    component: PasswordToggleField,
    supportedTypes: ["char"],
};

registry.category("fields").add("password_toggle", passwordToggleField);